# -*- coding: utf-8 -*-
'''
Created on 9 Aug 2014

@author: Kimon Tsitsikas and Éric Piel

Copyright © 2012-2014 Kimon Tsitsikas, Éric Piel, Delmic

This file is part of Odemis.

Odemis is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License version 2 as published by the Free Software
Foundation.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
Odemis. If not, see http://www.gnu.org/licenses/.
'''

from __future__ import division

from past.builtins import basestring
import collections
from concurrent import futures
import copy
import logging
import math
import numbers
import numpy
import itertools
from odemis import model, util
from odemis.model import (CancellableThreadPoolExecutor, CancellableFuture,
                          isasync, MD_PIXEL_SIZE_COR, MD_ROTATION_COR, MD_POS_COR)
import threading


class MultiplexActuator(model.Actuator):
    """
    An object representing an actuator made of several (real actuators)
     = a set of axes that can be moved and optionally report their position.
    """

    def __init__(self, name, role, dependencies, axes_map, ref_on_init=None, **kwargs):
        """
        name (string)
        role (string)
        dependencies (dict str -> actuator): axis name (in this actuator) -> actuator to be used for this axis
        axes_map (dict str -> str): axis name in this actuator -> axis name in the dependency actuator
        ref_on_init (None, list or dict (str -> float or None)): axes to be referenced during
          initialization. If it's a dict, it will go the indicated position
          after referencing, otherwise, it'll stay where it is.
        """
        if not dependencies:
            raise ValueError("MultiplexActuator needs dependencies")

        if set(dependencies.keys()) != set(axes_map.keys()):
            raise ValueError("MultiplexActuator needs the same keys in dependencies and axes_map")

        # Convert ref_on_init list to dict with no explicit move after
        if isinstance(ref_on_init, list):
            ref_on_init = {a: None for a in ref_on_init}
        self._ref_on_init = ref_on_init or {}
        self._axis_to_dep = {}  # axis name => (Actuator, axis name)
        self._position = {}
        self._speed = {}
        self._referenced = {}
        axes = {}

        for axis, dep in dependencies.items():
            caxis = axes_map[axis]
            self._axis_to_dep[axis] = (dep, caxis)

            # Ducktyping (useful to support also testing with MockComponent)
            # At least, it has .axes
            if not isinstance(dep, model.ComponentBase):
                raise ValueError("Dependency %s is not a component." % (dep,))
            if not hasattr(dep, "axes") or not isinstance(dep.axes, dict):
                raise ValueError("Dependency %s is not an actuator." % dep.name)
            axes[axis] = copy.deepcopy(dep.axes[caxis])
            self._position[axis] = dep.position.value[axes_map[axis]]
            if model.hasVA(dep, "speed") and caxis in dep.speed.value:
                self._speed[axis] = dep.speed.value[caxis]
            if model.hasVA(dep, "referenced") and caxis in dep.referenced.value:
                self._referenced[axis] = dep.referenced.value[caxis]

        # this set ._axes and ._dependencies
        model.Actuator.__init__(self, name, role, axes=axes,
                                dependencies=dependencies, **kwargs)

        if len(self.dependencies.value) > 1:
            # will take care of executing axis move asynchronously
            self._executor = CancellableThreadPoolExecutor(max_workers=1)  # one task at a time
            # TODO: make use of the 'Cancellable' part (for now cancelling a running future doesn't work)
        else:  # Only one dependency => optimize by passing all requests directly
            self._executor = None

        # keep a reference to the subscribers so that they are not
        # automatically garbage collected
        self._subfun = []

        dependencies_axes = {}  # dict actuator -> set of string (our axes)
        for axis, (dep, ca) in self._axis_to_dep.items():
            logging.debug("adding axis %s to dependency %s", axis, dep.name)
            if dep in dependencies_axes:
                dependencies_axes[dep].add(axis)
            else:
                dependencies_axes[dep] = {axis}

        # position & speed: special VAs combining multiple VAs
        self.position = model.VigilantAttribute(self._position, readonly=True)
        for c, ax in dependencies_axes.items():

            def update_position_per_dep(value, ax=ax, c=c):
                logging.debug("updating position of dependency %s", c.name)
                for a in ax:
                    try:
                        self._position[a] = value[axes_map[a]]
                    except KeyError:
                        logging.error("Dependency %s is not reporting position of axis %s", c.name, a)
                self._updatePosition()

            c.position.subscribe(update_position_per_dep)
            self._subfun.append(update_position_per_dep)

        # TODO: change the speed range to a dict of speed ranges
        self.speed = model.MultiSpeedVA(self._speed, [0., 10.], setter=self._setSpeed)
        for axis in self._speed.keys():
            c, ca = self._axis_to_dep[axis]

            def update_speed_per_dep(value, a=axis, ca=ca, cname=c.name):
                try:
                    self._speed[a] = value[ca]
                except KeyError:
                    logging.error("Dependency %s is not reporting speed of axis %s (%s): %s", cname, a, ca, value)
                self._updateSpeed()

            c.speed.subscribe(update_speed_per_dep)
            self._subfun.append(update_speed_per_dep)

        # whether the axes are referenced
        self.referenced = model.VigilantAttribute(self._referenced.copy(), readonly=True)

        for axis in self._referenced.keys():
            c, ca = self._axis_to_dep[axis]

            def update_ref_per_dep(value, a=axis, ca=ca, cname=c.name):
                try:
                    self._referenced[a] = value[ca]
                except KeyError:
                    logging.error("Dependency %s is not reporting reference of axis %s (%s)", cname, a, ca)
                self._updateReferenced()

            c.referenced.subscribe(update_ref_per_dep)
            self._subfun.append(update_ref_per_dep)

        for axis, pos in self._ref_on_init.items():
            # If the axis can be referenced => do it now (and move to a known position)
            if axis not in self._referenced:
                raise ValueError("Axis '%s' cannot be referenced, while should be referenced at init" % (axis,))
            if not self._referenced[axis]:
                # The initialisation will not fail if the referencing fails, but
                # the state of the component will be updated
                def _on_referenced(future, axis=axis):
                    try:
                        future.result()
                    except Exception as e:
                        c, ca = self._axis_to_dep[axis]
                        c.stop({ca})  # prevent any move queued
                        self.state._set_value(e, force_write=True)
                        logging.exception(e)

                f = self.reference({axis})
                f.add_done_callback(_on_referenced)

            # If already referenced => directly move
            # otherwise => put move on the queue, so that any move by client will
            # be _after_ the init position.
            if pos is not None:
                self.moveAbs({axis: pos})

    def _updatePosition(self):
        """
        update the position VA
        """
        # it's read-only, so we change it via _value
        pos = self._applyInversion(self._position)
        logging.debug("reporting position %s", pos)
        self.position._set_value(pos, force_write=True)

    def _updateSpeed(self):
        """
        update the speed VA
        """
        # we must not call the setter, so write directly the raw value
        self.speed._value = self._speed
        self.speed.notify(self._speed)

    def _updateReferenced(self):
        """
        update the referenced VA
        """
        # .referenced is copied to detect changes to it on next update
        self.referenced._set_value(self._referenced.copy(), force_write=True)

    def _setSpeed(self, value):
        """
        value (dict string-> float): speed for each axis
        returns (dict string-> float): the new value
        """
        # FIXME the problem with this implementation is that the subscribers
        # will receive multiple notifications for each set:
        # * one for each axis (via _updateSpeed from each dep)
        # * the actual one (but it's probably dropped as it's the same value)
        final_value = value.copy()  # copy
        for axis, v in value.items():
            dep, ma = self._axis_to_dep[axis]
            new_speed = dep.speed.value.copy()  # copy
            new_speed[ma] = v
            dep.speed.value = new_speed
            final_value[axis] = dep.speed.value[ma]
        return final_value

    def _moveTodepMove(self, mv, rel):
        """
        mv (dict str->value)
        rel (bool): indicate whether the move is relative or absolute
        return (dict str -> dict): dependency component -> move argument
        """
        dep_to_move = collections.defaultdict(dict)  # dep -> moveRel/moveAbs argument
        for axis, distance in mv.items():
            dep, dep_axis = self._axis_to_dep[axis]
            dep_to_move[dep].update({dep_axis: distance})
            logging.debug("Moving axis %s (-> %s) %s %g", axis, dep_axis,
                          "by" if rel else "to", distance)

        return dep_to_move

    def _axesTodepAxes(self, axes):
        dep_to_axes = collections.defaultdict(set)  # dep -> set(str): axes
        for axis in axes:
            dep, dep_axis = self._axis_to_dep[axis]
            dep_to_axes[dep].add(dep_axis)
            logging.debug("Interpreting axis %s (-> %s)", axis, dep_to_axes)

        return dep_to_axes

    @isasync
    def moveRel(self, shift, **kwargs):
        """
        Move the stage the defined values in m for each axis given.
        shift dict(string-> float): name of the axis and shift in m
        **kwargs: Mostly there to support "update" argument (but currently works
          only if there is only one dep)
        """
        if not shift:
            return model.InstantaneousFuture()
        self._checkMoveRel(shift)
        shift = self._applyInversion(shift)

        if self._executor:
            f = self._executor.submit(self._doMoveRel, shift, **kwargs)
        else:
            cmv = self._moveTodepMove(shift, rel=True)
            dep, move = cmv.popitem()
            assert not cmv
            f = dep.moveRel(move, **kwargs)

        return f

    def _doMoveRel(self, shift, **kwargs):
        # TODO: updates don't work because we still wait for the end of the
        # move before we get to the next one => multi-threaded queue? Still need
        # to ensure the order (ie, X>AB>X can be executed as X/AB>X or X>AB/X but
        # XA>AB>X must be in the order XA>AB/X
        futures = []
        for dep, move in self._moveTodepMove(shift, rel=True).items():
            f = dep.moveRel(move, **kwargs)
            futures.append(f)

        # just wait for all futures to finish
        for f in futures:
            f.result()

    @isasync
    def moveAbs(self, pos, **kwargs):
        if not pos:
            return model.InstantaneousFuture()
        self._checkMoveAbs(pos)
        pos = self._applyInversion(pos)

        if self._executor:
            f = self._executor.submit(self._doMoveAbs, pos, **kwargs)
        else:
            cmv = self._moveTodepMove(pos, rel=False)
            dep, move = cmv.popitem()
            assert not cmv
            f = dep.moveAbs(move, **kwargs)

        return f

    def _doMoveAbs(self, pos, **kwargs):
        futures = []
        for dep, move in self._moveTodepMove(pos, rel=False).items():
            f = dep.moveAbs(move, **kwargs)
            futures.append(f)

        # just wait for all futures to finish
        for f in futures:
            f.result()

    @isasync
    def reference(self, axes):
        if not axes:
            return model.InstantaneousFuture()
        self._checkReference(axes)
        if self._executor:
            f = self._executor.submit(self._doReference, axes)
        else:
            cmv = self._axesTodepAxes(axes)
            dep, a = cmv.popitem()
            assert not cmv
            f = dep.reference(a)

        return f
    reference.__doc__ = model.Actuator.reference.__doc__

    def _doReference(self, axes):
        dep_to_axes = self._axesTodepAxes(axes)
        futures = []
        for dep, a in dep_to_axes.items():
            f = dep.reference(a)
            futures.append(f)

        # just wait for all futures to finish
        for f in futures:
            f.result()

    def stop(self, axes=None):
        """
        stops the motion
        axes (iterable or None): list of axes to stop, or None if all should be stopped
        """
        # Empty the queue for the given axes
        if self._executor:
            self._executor.cancel()

        all_axes = set(self.axes.keys())
        axes = axes or all_axes
        unknown_axes = axes - all_axes
        if unknown_axes:
            logging.error("Attempting to stop unknown axes: %s", ", ".join(unknown_axes))
            axes &= all_axes

        threads = []
        for dep, a in self._axesTodepAxes(axes).items():
            # it's synchronous, but we want to stop all of them as soon as possible
            thread = threading.Thread(name="Stopping axis", target=dep.stop, args=(a,))
            thread.start()
            threads.append(thread)

        # wait for completion
        for thread in threads:
            thread.join(1)
            if thread.is_alive():
                logging.warning("Stopping dependency actuator of '%s' is taking more than 1s", self.name)

    def terminate(self):
        if self._executor:
            self.stop()
            self._executor.shutdown()
            self._executor = None


class CoupledStage(model.Actuator):
    """
    Wrapper stage that takes as dependencies the SEM sample stage and the
    ConvertStage. For each move to be performed CoupledStage moves, at the same
    time, both stages.
    """

    def __init__(self, name, role, dependencies, **kwargs):
        """
        dependencies (dict str -> actuator): names to ConvertStage and SEM sample stage
        """
        # SEM stage
        self._master = None
        # Optical stage
        self._slave = None

        for crole, dep in dependencies.items():
            # Check if dependencies are actuators
            if not isinstance(dep, model.ComponentBase):
                raise ValueError("Dependency %s is not a component." % dep)
            if not hasattr(dep, "axes") or not isinstance(dep.axes, dict):
                raise ValueError("Dependency %s is not an actuator." % dep.name)
            if "x" not in dep.axes or "y" not in dep.axes:
                raise ValueError("Dependency %s doesn't have both x and y axes" % dep.name)

            if crole == "slave":
                self._slave = dep
            elif crole == "master":
                self._master = dep
            else:
                raise ValueError("Dependency given to CoupledStage must be either 'master' or 'slave', but got %s." % crole)

        if self._master is None:
            raise ValueError("CoupledStage needs a master dependency.")
        if self._slave is None:
            raise ValueError("CoupledStage needs a slave dependency.")

        # TODO: limit the range to the minimum of master/slave?
        axes_def = {}
        for an in ("x", "y"):
            axes_def[an] = copy.deepcopy(self._master.axes[an])
            axes_def[an].canUpdate = False

        model.Actuator.__init__(self, name, role, axes=axes_def, dependencies=dependencies,
                                **kwargs)
        self._metadata[model.MD_HW_NAME] = "CoupledStage"

        # will take care of executing axis moves asynchronously
        self._executor = CancellableThreadPoolExecutor(max_workers=1)  # one task at a time

        self._position = {}
        # RO, as to modify it the client must use .moveRel() or .moveAbs()
        self.position = model.VigilantAttribute({}, unit="m", readonly=True)
        self._updatePosition()
        # TODO: listen to master position to update the position? => but
        # then it might get updated too early, before the slave has finished
        # moving.

        self.referenced = model.VigilantAttribute({}, readonly=True)
        # listen to changes from dependencies
        for c in self.dependencies.value:
            if model.hasVA(c, "referenced"):
                logging.debug("Subscribing to reference of dependency %s", c.name)
                c.referenced.subscribe(self._ondepReferenced)
        self._updateReferenced()

        self._stage_conv = None
        self._createConvertStage()

    def updateMetadata(self, md):
        self._metadata.update(md)
        # Re-initialize ConvertStage with the new transformation values
        # Called after every sample holder insertion
        self._createConvertStage()

    def _createConvertStage(self):
        """
        (Re)create the convert stage, based on the metadata
        """
        self._stage_conv = ConvertStage("converter-xy", "align",
                    dependencies={"aligner": self._slave},
                    axes=["x", "y"],
                    scale=self._metadata.get(MD_PIXEL_SIZE_COR, (1, 1)),
                    rotation=self._metadata.get(MD_ROTATION_COR, 0),
                    translation=self._metadata.get(MD_POS_COR, (0, 0)))

#         if set(self._metadata.keys()) & {MD_PIXEL_SIZE_COR, MD_ROTATION_COR, MD_POS_COR}:
#             # Schedule a null relative move, just to ensure the stages are
#             # synchronised again (if some metadata is provided)
#             self._executor.submit(self._doMoveRel, {})

    def _updatePosition(self):
        """
        update the position VA
        """
        mode_pos = self._master.position.value
        self._position["x"] = mode_pos['x']
        self._position["y"] = mode_pos['y']

        pos = self._applyInversion(self._position)
        self.position._set_value(pos, force_write=True)

    def _ondepReferenced(self, ref):
        # ref can be from any dep, so we don't use it
        self._updateReferenced()

    def _updateReferenced(self):
        """
        update the referenced VA
        """
        ref = {} # str (axes name) -> boolean (is referenced)
        # consider an axis referenced iff it's referenced in every referenceable dependencies
        for c in self.dependencies.value:
            if not model.hasVA(c, "referenced"):
                continue
            cref = c.referenced.value
            for a in (set(self.axes.keys()) & set(cref.keys())):
                ref[a] = ref.get(a, True) and cref[a]

        self.referenced._set_value(ref, force_write=True)

    def _doMoveAbs(self, pos):
        """
        move to the position
        """
        f = self._master.moveAbs(pos)
        try:
            f.result()
        finally:  # synchronise slave position even if move failed
            # TODO: Move simultaneously based on the expected position, and
            # only if the final master position is different, move again.
            mpos = self._master.position.value
            # Move objective lens
            f = self._stage_conv.moveAbs({"x": mpos["x"], "y": mpos["y"]})
            f.result()

        self._updatePosition()

    def _doMoveRel(self, shift):
        """
        move by the shift
        """
        f = self._master.moveRel(shift)
        try:
            f.result()
        finally:
            mpos = self._master.position.value
            # Move objective lens
            f = self._stage_conv.moveAbs({"x": mpos["x"], "y": mpos["y"]})
            f.result()

        self._updatePosition()

    @isasync
    def moveRel(self, shift):
        if not shift:
            shift = {"x": 0, "y": 0}
        self._checkMoveRel(shift)

        shift = self._applyInversion(shift)
        return self._executor.submit(self._doMoveRel, shift)

    @isasync
    def moveAbs(self, pos):
        if not pos:
            pos = self.position.value
        self._checkMoveAbs(pos)
        pos = self._applyInversion(pos)

        return self._executor.submit(self._doMoveAbs, pos)

    def stop(self, axes=None):
        # Empty the queue for the given axes
        self._executor.cancel()
        self._master.stop(axes)
        self._stage_conv.stop(axes)
        logging.info("Stopping all axes: %s", ", ".join(axes or self.axes))

    def _doReference(self, axes):
        fs = []
        for c in self.dependencies.value:
            # only do the referencing for the stages that support it
            if not model.hasVA(c, "referenced"):
                continue
            ax = axes & set(c.referenced.value.keys())
            fs.append(c.reference(ax))

        # wait for all referencing to be over
        for f in fs:
            f.result()

        # Re-synchronize the 2 stages by moving the slave where the master is
        mpos = self._master.position.value
        f = self._stage_conv.moveAbs({"x": mpos["x"], "y": mpos["y"]})
        f.result()

        self._updatePosition()

    @isasync
    def reference(self, axes):
        if not axes:
            return model.InstantaneousFuture()
        self._checkReference(axes)
        return self._executor.submit(self._doReference, axes)

    def terminate(self):
        if self._executor:
            self.stop()
            self._executor.shutdown()
            self._executor = None


class ConvertStage(model.Actuator):
    """
    Stage wrapper component with X/Y axis that converts the target sample stage
    position coordinates to the objective lens position based one a given scale,
    offset and rotation. This way it takes care of maintaining the alignment of
    the two stages, as for each SEM stage move it is able to perform the
    corresponding “compensate” move in objective lens.
    """

    def __init__(self, name, role, dependencies, axes,
                 rotation=0, scale=None, translation=None, **kwargs):
        """
        dependencies (dict str -> actuator): name to objective lens actuator
        axes (list of 2 strings): names of the axes for x and y
        scale (None tuple of 2 floats): scale factor from exported to original position
        rotation (float): rotation factor (in radians)
        translation (None or tuple of 2 floats): translation offset (in m)
        """
        assert len(axes) == 2
        if len(dependencies) != 1:
            raise ValueError("ConvertStage needs 1 dependency")

        self._dependency = list(dependencies.values())[0]
        self._axes_dep = {"x": axes[0], "y": axes[1]}
        if scale is None:
            scale = (1, 1)
        if translation is None:
            translation = (0, 0)

        # TODO: range of axes could at least be updated with scale + translation
        # and (when there is rotation) canUpdate would only be True if both axes
        # canUpdate.
        axes_def = {"x": self._dependency.axes[axes[0]],
                    "y": self._dependency.axes[axes[1]]}
        model.Actuator.__init__(self, name, role, axes=axes_def, **kwargs)

        self._metadata[model.MD_POS_COR] = translation
        self._metadata[model.MD_ROTATION_COR] = rotation
        self._metadata[model.MD_PIXEL_SIZE_COR] = scale
        self._updateConversion()

        # RO, as to modify it the client must use .moveRel() or .moveAbs()
        self.position = model.VigilantAttribute({"x": 0, "y": 0},
                                                unit="m", readonly=True)
        # it's just a conversion from the dep's position
        self._dependency.position.subscribe(self._updatePosition, init=True)

        # Speed & reference: it's complicated => user should look at the dep

    def _updateConversion(self):
        translation = self._metadata[model.MD_POS_COR]
        rotation = self._metadata[model.MD_ROTATION_COR]
        scale = self._metadata[model.MD_PIXEL_SIZE_COR]
        # Rotation * scaling for convert back/forth between exposed and dep
        self._Mtodep = numpy.array(
                     [[math.cos(rotation) * scale[0], -math.sin(rotation) * scale[0]],
                      [math.sin(rotation) * scale[1], math.cos(rotation) * scale[1]]])

        self._Mfromdep = numpy.array(
                     [[math.cos(-rotation) / scale[0], -math.sin(-rotation) / scale[1]],
                      [math.sin(-rotation) / scale[0], math.cos(-rotation) / scale[1]]])

        # Offset between origins of the coordinate systems
        self._O = numpy.array([translation[0], translation[1]], dtype=numpy.float)

    def _convertPosFromdep(self, pos_dep, absolute=True):
        # Object lens position vector
        Q = numpy.array([pos_dep[0], pos_dep[1]], dtype=numpy.float)
        # Transform to coordinates in the reference frame of the sample stage
        p = self._Mfromdep.dot(Q)
        if absolute:
            p -= self._O
        return p.tolist()

    def _convertPosTodep(self, pos, absolute=True):
        # Sample stage position vector
        P = numpy.array([pos[0], pos[1]], dtype=numpy.float)
        if absolute:
            P += self._O
        # Transform to coordinates in the reference frame of the objective stage
        q = self._Mtodep.dot(P)
        return q.tolist()

    def _updatePosition(self, pos_dep):
        """
        update the position VA when the dep's position is updated
        """
        vpos_dep = [pos_dep[self._axes_dep["x"]],
                      pos_dep[self._axes_dep["y"]]]
        vpos = self._convertPosFromdep(vpos_dep)
        # it's read-only, so we change it via _value
        self.position._set_value({"x": vpos[0], "y": vpos[1]}, force_write=True)

    def updateMetadata(self, md):
        self._metadata.update(md)
        self._updateConversion()
        self._updatePosition(self._dependency.position.value)

    @isasync
    def moveRel(self, shift, **kwargs):
        """
        **kwargs: Mostly there to support "update" argument
        """
        # shift is a vector, so relative conversion
        vshift = shift.get("x", 0), shift.get("y", 0)
        vshift_dep = self._convertPosTodep(vshift, absolute=False)

        shift_dep = {self._axes_dep["x"]: vshift_dep[0],
                       self._axes_dep["y"]: vshift_dep[1]}
        logging.debug("converted relative move from %s to %s", shift, shift_dep)
        f = self._dependency.moveRel(shift_dep, **kwargs)
        return f

    @isasync
    def moveAbs(self, pos, **kwargs):
        """
        **kwargs: Mostly there to support "update" argument
        """
        # pos is a position, so absolute conversion
        cpos = self.position.value
        vpos = pos.get("x", cpos["x"]), pos.get("y", cpos["y"])
        vpos_dep = self._convertPosTodep(vpos)

        pos_dep = {self._axes_dep["x"]: vpos_dep[0],
                     self._axes_dep["y"]: vpos_dep[1]}
        logging.debug("converted absolute move from %s to %s", pos, pos_dep)
        f = self._dependency.moveAbs(pos_dep, **kwargs)
        return f

    def stop(self, axes=None):
        self._dependency.stop()

    @isasync
    def reference(self, axes):
        f = self._dependency.reference(axes)
        return f


class AntiBacklashActuator(model.Actuator):
    """
    This is a stage wrapper that takes a stage and ensures that every move
    always finishes in the same direction.
    """

    def __init__(self, name, role, dependencies, backlash, **kwargs):
        """
        dependencies (dict str -> Stage): dict containing one component, the stage
        to wrap
        backlash (dict str -> float): for each axis of the stage, the additional
        distance to move (and the direction). If an axis of the stage is not
        present, then it’s the same as having 0 as backlash (=> no antibacklash 
        motion is performed for this axis)

        """
        if len(dependencies) != 1:
            raise ValueError("AntiBacklashActuator needs 1 dependency")

        for a, v in backlash.items():
            if not isinstance(a, basestring):
                raise ValueError("Backlash key must be a string but got '%s'" % (a,))
            if not isinstance(v, numbers.Real):
                raise ValueError("Backlash value of %s must be a number but got '%s'" % (a, v))

        self._dependency = list(dependencies.values())[0]
        self._backlash = backlash
        axes_def = {}
        for an, ax in self._dependency.axes.items():
            axes_def[an] = copy.deepcopy(ax)
            axes_def[an].canUpdate = True
            if an in backlash and hasattr(ax, "range"):
                # Restrict the range to have some margin for the anti-backlash move
                rng = ax.range
                if rng[1] - rng[0] < abs(backlash[an]):
                    raise ValueError("Backlash of %g m is bigger than range %s" %
                                     (backlash[an], rng))
                if backlash[an] > 0:
                    axes_def[an].range = (rng[0] + backlash[an], rng[1])
                else:
                    axes_def[an].range = (rng[0], rng[1] + backlash[an])

        # Whether currently a backlash shift is applied on an axis
        # If True, moving the axis by the backlash value would restore its expected position
        # _shifted_lock must be taken before modifying this attribute
        self._shifted = {a: False for a in axes_def.keys()}
        self._shifted_lock = threading.Lock()

        # look for axes in backlash not existing in the dep
        missing = set(backlash.keys()) - set(axes_def.keys())
        if missing:
            raise ValueError("Dependency actuator doesn't have the axes %s" % (missing,))

        model.Actuator.__init__(self, name, role, axes=axes_def,
                                dependencies=dependencies, **kwargs)

        # will take care of executing axis moves asynchronously
        self._executor = CancellableThreadPoolExecutor(max_workers=1)  # one task at a time

        # Duplicate VAs which are just identical
        # TODO: shall we "hide" the antibacklash move by not updating position
        # while doing this move?
        self.position = self._dependency.position

        if model.hasVA(self._dependency, "referenced"):
            self.referenced = self._dependency.referenced
        if model.hasVA(self._dependency, "speed"):
            self.speed = self._dependency.speed

    def terminate(self):
        if self._executor:
            self.stop()
            self._executor.shutdown()
            self._executor = None

    def _antiBacklashMove(self, axes):
        """
        Moves back the axes to their official position by reverting the anti-backlash shift
        axes (list of str): the axes to revert
        """
        sub_backlash = {}  # same as backlash but only contains the axes moved
        with self._shifted_lock:
            for a in axes:
                if self._shifted[a]:
                    if a in self._backlash:
                        sub_backlash[a] = self._backlash[a]
                    self._shifted[a] = False

        if sub_backlash:
            logging.debug("Running anti-backlash move %s", sub_backlash)
            self._dependency.moveRelSync(sub_backlash)

    def _doMoveRel(self, future, shift):
        # move with the backlash subtracted
        sub_shift = {}
        for a, v in shift.items():
            if a not in self._backlash:
                sub_shift[a] = v
            else:
                # optimisation: if move goes in the same direction as backlash
                # correction, then no need to do the correction
                # TODO: only do this if backlash correction has already been applied once?
                if v * self._backlash[a] >= 0:
                    sub_shift[a] = v
                else:
                    with self._shifted_lock:
                        if self._shifted[a]:
                            sub_shift[a] = v
                        else:
                            sub_shift[a] = v - self._backlash[a]
                            self._shifted[a] = True

        # Do the backlash + move
        axes = set(shift.keys())
        if not any(self._shifted):
            # a tiny bit faster as we don't sleep
            self._dependency.moveRelSync(sub_shift)
        else:
            # some antibacklash move needed afterwards => update might be worthy
            f = self._dependency.moveRel(sub_shift)
            done = False
            while not done:
                try:
                    f.result(timeout=0.01)
                except futures.TimeoutError:
                    pass  # Keep waiting for end of move
                else:
                    done = True

                # Check if there is already a new move to do
                nf = self._executor.get_next_future(future)
                if nf is not None and axes <= nf._update_axes:
                    logging.debug("Ending move control early as next move is an update containing %s", axes)
                    return

        # backlash move
        self._antiBacklashMove(list(shift.keys()))

    def _doMoveAbs(self, future, pos):
        sub_pos = {}
        for a, v in pos.items():
            if a not in self._backlash:
                sub_pos[a] = v
            else:
                shift = v - self.position.value[a]
                with self._shifted_lock:
                    if shift * self._backlash[a] >= 0:
                        sub_pos[a] = v
                        self._shifted[a] = False
                    else:
                        sub_pos[a] = v - self._backlash[a]
                        self._shifted[a] = True

        # Do the backlash + move
        axes = set(pos.keys())
        if not any(self._shifted):
            # a tiny bit faster as we don't sleep
            self._dependency.moveAbsSync(sub_pos)
        else:  # some antibacklash move needed afterwards => update might be worthy
            f = self._dependency.moveAbs(sub_pos)
            done = False
            while not done:
                try:
                    f.result(timeout=0.01)
                except futures.TimeoutError:
                    pass  # Keep waiting for end of move
                else:
                    done = True

                # Check if there is already a new move to do
                nf = self._executor.get_next_future(future)
                if nf is not None and axes <= nf._update_axes:
                    logging.debug("Ending move control early as next move is an update containing %s", axes)
                    return

        # anti-backlash move
        self._antiBacklashMove(axes)

    def _createFuture(self, axes, update):
        """
        Return (CancellableFuture): a future that can be used to manage a move
        axes (set of str): the axes that are moved
        update (bool): if it's an update move
        """
        # TODO: do this via the __init__ of subclass of Future?
        f = CancellableFuture()  # TODO: make it cancellable too

        f._update_axes = set()  # axes handled by the move, if update
        if update:
            # Check if all the axes support it
            if all(self.axes[a].canUpdate for a in axes):
                f._update_axes = axes
            else:
                logging.warning("Trying to do a update move on axes %s not supporting update", axes)

        return f

    @isasync
    def moveRel(self, shift, update=False):
        if not shift:
            return model.InstantaneousFuture()
        self._checkMoveRel(shift)

        f = self._createFuture(set(shift.keys()), update)
        return self._executor.submitf(f, self._doMoveRel, f, shift)

    @isasync
    def moveAbs(self, pos, update=False):
        if not pos:
            return model.InstantaneousFuture()
        self._checkMoveAbs(pos)

        f = self._createFuture(set(pos.keys()), update)
        return self._executor.submitf(f, self._doMoveAbs, f, pos)

    def stop(self, axes=None):
        self._dependency.stop(axes=axes)

    @isasync
    def reference(self, axes):
        f = self._dependency.reference(axes)
        return f


class LinearActuator(model.Actuator):
    """
    A generic actuator component which allows moving on a linear axis. It is actually a
    wrapper to just one axis/actuator. The actuator is referenced after n moves (can
    be specified in constructor).
    """

    def __init__(self, name, role, dependencies, axis_name, offset=0,
                 ref_start=None, ref_period=10, inverted=None, **kwargs):
        """
        name (string)
        role (string)
        dependencies (dict str -> actuator): axis name (in this actuator) -> actuator to be used for this axis
        axis_name (str): axis name in the dependency actuator
        offset (float): axis offset (negative of reference position value)
        ref_start (float or None): Value usually chosen close to reference switch from where to start
         referencing. Used to optimize runtime for referencing. If None, value will be 5% of value of cycle.
        ref_period (int or None): number of moves before referencing axis, None to disable
         automatic referencing
        """
        if inverted:
            raise ValueError("Axes shouldn't be inverted")

        if len(dependencies) != 1:
            raise ValueError("LinearActuator needs precisely one dependency")

        axis, dep = list(dependencies.items())[0]
        self._axis = axis
        self._dependency = dep
        self._caxis = axis_name
        self._offset = offset
        self._ref_start = ref_start
        self._ref_period = ref_period  # number of moves before automatic referencing
        self._move_num = 0  # current number of moves after last referencing

        # Executor used to reference and move to nearest position
        self._executor = CancellableThreadPoolExecutor(max_workers=1)  # one task at a time

        if not isinstance(dep, model.ComponentBase):
            raise ValueError("Dependency %s is not a component." % (dep,))
        if not hasattr(dep, "axes") or not isinstance(dep.axes, dict):
            raise ValueError("Dependency %s is not an actuator." % dep.name)

        ac = dep.axes[axis_name]
        rng = (ac.range[0] - self._offset, ac.range[1] - self._offset)
        axes = {axis: model.Axis(range=rng, unit=ac.unit)}
        model.Actuator.__init__(self, name, role, axes=axes, dependencies=dependencies, **kwargs)

        # Offset from which to start referencing
        if self._ref_start is None:
            self._ref_start = abs(rng[1] - rng[0]) * 0.05 - self._offset
        if not rng[0] <= self._ref_start <= rng[1]:
            raise ValueError("Reference start needs to be between %s and %s. " % (rng[0], rng[1]) +
                             "Got value %s." % self._ref_start)

        self.position = model.VigilantAttribute({}, readonly=True)
        logging.debug("Subscribing to position of dependency %s", dep.name)
        dep.position.subscribe(self._update_dep_position, init=True)

        referenced = False
        if model.hasVA(dep, "referenced") and axis_name in dep.referenced.value:
            referenced = dep.referenced.value[axis_name]
            self.referenced = model.VigilantAttribute({self._axis: referenced}, readonly=True)
            dep.referenced.subscribe(self._update_dep_ref)
        if not referenced:
            # The initialisation will not fail if the referencing fails
            f = self.reference({axis})
            f.add_done_callback(self._on_referenced)
        else:
            self.moveAbs({self._axis: self._dependency.position.value[self._caxis]}).result()

    def _on_referenced(self, future):
        try:
            future.result()
        except Exception as e:
            self._dependency.stop({self._caxis})  # prevent any move queued
            self.state._set_value(e, force_write=True)
            logging.exception(e)

    def _update_dep_position(self, value):
        pos = value[self._caxis] - self._offset
        self.position._set_value({self._axis: pos}, force_write=True)

    def _update_dep_ref(self, value):
        referenced = value[self._caxis]
        self.referenced._set_value({self._axis: referenced}, force_write=True)

    @isasync
    def moveRel(self, shift):
        """
        Move the rotation actuator by a defined shift.
        shift dict(string-> float): name of the axis and shift
        """
        if not shift:
            return model.InstantaneousFuture()
        self._checkMoveRel(shift)
        f = self._executor.submit(self._doMoveRel, shift)

        return f

    @isasync
    def moveAbs(self, pos):
        """
        Move the actuator to the defined position in m for each axis given.
        pos dict(string-> float): name of the axis and position in m
        """

        if not pos:
            return model.InstantaneousFuture()
        self._checkMoveAbs(pos)
        pos = self._applyInversion(pos)
        f = self._executor.submit(self._doMoveAbs, pos)

        return f

    def _referenceIfNeeded(self):
        """
        Force a referencing if it has moved a certain number of times
        """
        # Reference from time to time
        self._move_num += 1
        if self._ref_period and self._move_num > self._ref_period:
            # Axis might always go towards negative direction during referencing. Make
            # sure that the referencing works properly in case of a negative position (especially
            # important if the actuator is cyclic).
            logging.debug("Moving to reference starting position %s", self._ref_start)
            self._dependency.moveAbsSync({self._caxis: self._ref_start + self._offset})
            logging.debug("Referencing axis %s (-> %s) after %s moves", self._axis, self._caxis, self._move_num)
            self._dependency.reference({self._caxis}).result()
            self._move_num = 0

    def _doMoveRel(self, shift):
        """
        shift dict(string-> float): name of the axis and shift
        """
        self._referenceIfNeeded()
        self._dependency.moveRel({self._caxis: shift[self._axis]}).result()

    def _doMoveAbs(self, pos):
        self._referenceIfNeeded()

        axis, distance = list(pos.items())[0]
        logging.debug("Moving axis %s (-> %s) to %g", self._axis, self._caxis, distance)
        move = {self._caxis: distance + self._offset}
        self._dependency.moveAbs(move).result()

    def _doReference(self, axes):
        logging.debug("Referencing axis %s (-> %s)", self._axis, self._caxis)
        # Reset reference counter
        self._move_num = 0
        f = self._dependency.reference({self._caxis})
        f.result()

    @isasync
    def reference(self, axes):
        if not axes:
            return model.InstantaneousFuture()
        self._checkReference(axes)

        f = self._executor.submit(self._doReference, axes)
        return f

    reference.__doc__ = model.Actuator.reference.__doc__

    def stop(self):
        self._dependency.stop({self._caxis})

    def terminate(self):
        if self._executor:
            self.stop()
            self._executor.shutdown(wait=True)
            self._executor = None

        self._dependency.position.unsubscribe(self._update_dep_position)
        if hasattr(self, "referenced"):
            self._dependency.referenced.subscribe(self._update_dep_ref)


class FixedPositionsActuator(model.Actuator):
    """
    A generic actuator component which only allows moving to fixed positions
    defined by the user upon initialization. It is actually a wrapper to just
    one axis/actuator and it can also apply cyclic move e.g. in case the
    actuator moves a filter wheel.
    """

    def __init__(self, name, role, dependencies, axis_name, positions, cycle=None,
                 inverted=None, **kwargs):
        """
        name (string)
        role (string)
        dependencies (dict str -> actuator): axis name (in this actuator) -> actuator to be used for this axis
        axis_name (str): axis name in the dependency actuator
        positions (set or dict value -> str): positions where the actuator is allowed to move
        cycle (float): if not None, it means the actuator does a cyclic move and this value represents a full cycle
        """
        if inverted:
            raise ValueError("Axes shouldn't be inverted")

        if len(dependencies) != 1:
            raise ValueError("FixedPositionsActuator needs precisely one dependency.")

        self._cycle = cycle
        self._move_sum = 0
        self._position = {}
        self._referenced = {}
        axis, dep = list(dependencies.items())[0]
        self._axis = axis
        self._dependency = dep
        self._caxis = axis_name
        self._positions = positions
        # Executor used to reference and move to nearest position
        self._executor = CancellableThreadPoolExecutor(max_workers=1)  # one task at a time

        if not isinstance(dep, model.ComponentBase):
            raise ValueError("Dependency %s is not a component." % (dep,))
        if not hasattr(dep, "axes") or not isinstance(dep.axes, dict):
            raise ValueError("Dependency %s is not an actuator." % dep.name)

        if cycle is not None:
            # just an offset to reference switch position
            self._offset = self._cycle / len(self._positions)
            if not all(0 <= p < cycle for p in positions.keys()):
                raise ValueError("Positions must be between 0 and %s (non inclusive)" % (cycle,))

        ac = dep.axes[axis_name]
        axes = {axis: model.Axis(choices=positions, unit=ac.unit)}  # TODO: allow the user to override the unit?

        model.Actuator.__init__(self, name, role, axes=axes, dependencies=dependencies, **kwargs)

        self._position = {}
        self.position = model.VigilantAttribute({}, readonly=True)

        logging.debug("Subscribing to position of dependency %s", dep.name)
        dep.position.subscribe(self._update_dep_position, init=True)

        if model.hasVA(dep, "referenced") and axis_name in dep.referenced.value:
            self._referenced[axis] = dep.referenced.value[axis_name]
            self.referenced = model.VigilantAttribute(self._referenced.copy(), readonly=True)
            dep.referenced.subscribe(self._update_dep_ref)

        # If the axis can be referenced => do it now (and move to a known position)
        # In case of cyclic move always reference
        if not self._referenced.get(axis, True) or (self._cycle and axis in self._referenced):
            # The initialisation will not fail if the referencing fails
            f = self.reference({axis})
            f.add_done_callback(self._on_referenced)
        else:
            # If not at a known position => move to the closest known position
            nearest = util.find_closest(self._dependency.position.value[self._caxis], list(self._positions.keys()))
            self.moveAbs({self._axis: nearest}).result()

    def _on_referenced(self, future):
        try:
            future.result()
        except Exception as e:
            self._dependency.stop({self._caxis})  # prevent any move queued
            self.state._set_value(e, force_write=True)
            logging.exception(e)

    def _update_dep_position(self, value):
        p = value[self._caxis]
        if self._cycle is not None:
            p %= self._cycle
        self._position[self._axis] = p
        self._updatePosition()

    def _update_dep_ref(self, value):
        self._referenced[self._axis] = value[self._caxis]
        self._updateReferenced()

    def _updatePosition(self):
        """
        update the position VA
        """
        # if it is an unsupported position report the nearest supported one
        real_pos = self._position[self._axis]
        nearest = util.find_closest(real_pos, self._positions.keys())
        if not util.almost_equal(real_pos, nearest):
            logging.warning("Reporting axis %s @ %s (known position), while physical axis %s @ %s",
                            self._axis, nearest, self._caxis, real_pos)
        pos = {self._axis: nearest}
        logging.debug("reporting position %s", pos)
        self.position._set_value(pos, force_write=True)

    def _updateReferenced(self):
        """
        update the referenced VA
        """
        # .referenced is copied to detect changes to it on next update
        self.referenced._set_value(self._referenced.copy(), force_write=True)

    @isasync
    def moveRel(self, shift):
        if not shift:
            return model.InstantaneousFuture()
        self._checkMoveRel(shift)
        raise NotImplementedError("Relative move on fixed positions axis not supported")

    @isasync
    def moveAbs(self, pos):
        """
        Move the actuator to the defined position in m for each axis given.
        pos dict(string-> float): name of the axis and position in m
        """
        if not pos:
            return model.InstantaneousFuture()
        self._checkMoveAbs(pos)
        pos = self._applyInversion(pos)
        f = self._executor.submit(self._doMoveAbs, pos)

        return f

    def _doMoveAbs(self, pos):
        axis, distance = list(pos.items())[0]
        logging.debug("Moving axis %s (-> %s) to %g", self._axis, self._caxis, distance)

        try:
            # While it's moving, don't listen to the intermediary positions,
            # as it will not fit any known position, and be confusing.
            self._dependency.position.unsubscribe(self._update_dep_position)

            if self._cycle is None:
                move = {self._caxis: distance}
                self._dependency.moveAbs(move).result()
            else:
                # Optimize by moving through the closest way
                cur_pos = self._dependency.position.value[self._caxis]
                vector1 = distance - cur_pos
                mod1 = vector1 % self._cycle
                vector2 = cur_pos - distance
                mod2 = vector2 % self._cycle
                if abs(mod1) < abs(mod2):
                    self._move_sum += mod1
                    if self._move_sum >= self._cycle:
                        # Once we are about to complete a full cycle, reference again
                        # to get rid of accumulated error
                        self._move_sum = 0
                        # move to the reference switch
                        move_to_ref = (self._cycle - cur_pos) % self._cycle + self._offset
                        self._dependency.moveRel({self._caxis: move_to_ref}).result()
                        self._dependency.reference({self._caxis}).result()
                        move = {self._caxis: distance}
                    else:
                        move = {self._caxis: mod1}
                else:
                    move = {self._caxis: -mod2}
                    self._move_sum -= mod2

                self._dependency.moveRel(move).result()
        finally:
            self._dependency.position.subscribe(self._update_dep_position, init=True)

    def _doReference(self, axes):
        logging.debug("Referencing axis %s (-> %s)", self._axis, self._caxis)
        f = self._dependency.reference({self._caxis})
        f.result()

        # If we just did homing and ended up to an unsupported position, move to
        # the nearest supported position
        cp = self._dependency.position.value[self._caxis]
        if cp not in self._positions:
            nearest = util.find_closest(cp, self._positions.keys())
            self._doMoveAbs({self._axis: nearest})

    @isasync
    def reference(self, axes):
        if not axes:
            return model.InstantaneousFuture()
        self._checkReference(axes)

        f = self._executor.submit(self._doReference, axes)
        return f
    reference.__doc__ = model.Actuator.reference.__doc__

    def stop(self, axes=None):
        """
        stops the motion
        axes (iterable or None): list of axes to stop, or None if all should be stopped
        """
        if axes is not None:
            axes = set()
            if self._axis in axes:
                axes.add(self._caxis)

        self._dependency.stop(axes=axes)

    def terminate(self):
        if self._executor:
            self.stop()
            self._executor.shutdown(wait=True)
            self._executor = None

        self._dependency.position.unsubscribe(self._update_dep_position)
        if hasattr(self, "referenced"):
            self._dependency.referenced.subscribe(self._update_dep_ref)


class CombinedSensorActuator(model.Actuator):
    """
    An actuator component which allows moving to fixed positions which can
    be detected by a separate component.
    """

    def __init__(self, name, role, dependencies, axis_actuator, axis_sensor,
                 positions, to_sensor, inverted=None, **kwargs):
        """
        name (string)
        role (string)
        dependencies (dict str -> actuator): role (in this actuator) -> actuator
           "actuator": dependency used to move the axis
           "sensor: dependency used to read the position (via the .position)
        axis_actuator (str): axis name in the dependency actuator
        axis_sensor (str): axis name in the dependency sensor
        positions (set or dict value -> (str or [str])): positions where the actuator is allowed to move
        to_sensor (dict value -> value): position of the actuator to position reported by the sensor
        """
        if inverted:
            raise ValueError("Axes shouldn't be inverted")
        if len(dependencies) != 2:
            raise ValueError("CombinedSensorActuator needs precisely two dependencies")

        try:
            dep = dependencies["actuator"]
        except KeyError:
            raise ValueError("No 'actuator' dependency provided")
        if not isinstance(dep, model.ComponentBase):
            raise ValueError("Dependency %s is not a component." % (dep.name,))
        if not hasattr(dep, "axes") or not isinstance(dep.axes, dict):
            raise ValueError("Dependency %s is not an actuator." % dep.name)
        try:
            sensor = dependencies["sensor"]
        except KeyError:
            raise ValueError("No 'sensor' dependency provided")
        if not isinstance(sensor, model.ComponentBase):
            raise ValueError("Dependency %s is not a component." % (sensor.name,))
        if not model.hasVA(sensor, "position"):  # or not c in sensor.position.value:
            raise ValueError("Dependency %s has no position VA." % sensor.name)

        self._dependency = dep
        self._sensor = sensor

        self._axis = axis_actuator
        self._axis_sensor = axis_sensor
        ac = dep.axes[axis_actuator]
        axes = {self._axis: model.Axis(choices=positions, unit=ac.unit)}

        self._positions = positions
        self._to_sensor = to_sensor
        # Check that each actuator position in to_sensor is valid
        if set(to_sensor.keys()) != set(positions):
            raise ValueError("to_sensor doesn't contain the same values as 'positions'.")

        # Check that each sensor position in to_sensor is valid
        as_def = sensor.axes[self._axis_sensor]
        if hasattr(as_def, "choices"):
            if not set(to_sensor.values()) <= set(as_def.choices):
                raise ValueError("to_sensor doesn't contain the same values as available in the sensor (%s)." %
                                 (as_def.choices,))
        elif hasattr(as_def, "range"):
            if not all(as_def.range[0] <= p <= as_def.range[1] for p in to_sensor.values()):
                raise ValueError("to_sensor contains out-of-range values for the sensor (range is %s)." %
                                 (as_def.range,))

        # This is the compensation needed for the actuator to move to the expected
        # position. Will be updated after a move, if extra shift is needed.
        # TODO: also update during referencing
        self._pos_shift = 0  # in dependency actuator axis unit

        # Executor used to reference and move to nearest position
        self._executor = CancellableThreadPoolExecutor(max_workers=1)  # one task at a time

        model.Actuator.__init__(self, name, role, axes=axes, dependencies=dependencies, **kwargs)

        self.position = model.VigilantAttribute({}, readonly=True)
        logging.debug("Subscribing to position of dependencies %s and %s", dep.name, sensor.name)
        dep.position.subscribe(self._update_dep_position)
        sensor.position.subscribe(self._updatePosition, init=True)

        # TODO: provide our own reference?
        if model.hasVA(dep, "referenced") and axis_actuator in dep.referenced.value:
            self._referenced = {self._axis: dep.referenced.value[axis_actuator]}
            self.referenced = model.VigilantAttribute(self._referenced.copy(), readonly=True)
            dep.referenced.subscribe(self._update_dep_ref)

    def _update_dep_position(self, value):
        # Force reading the sensor position
        self._updatePosition()

    def _update_dep_ref(self, value):
        self._referenced[self._axis] = value[self._axis]
        self._updateReferenced()

    def _updatePosition(self, spos=None):
        """
        update the position VA
        spos: position from the sensor
        """
        if spos is None:
            spos = self._sensor.position.value

        spos_axis = spos[self._axis_sensor]

        # Convert from sensor to "actuator" position
        for ap, sp in self._to_sensor.items():
            if sp == spos_axis:
                pos = {self._axis: ap}
                break
        else:
            logging.error("No equivalent position known for sensor position %s", spos_axis)
            # TODO: look for the closest one?
            return

        logging.debug("Reporting position %s", pos)
        self.position._set_value(pos, force_write=True)

    def _updateReferenced(self):
        """
        update the referenced VA
        """
        # .referenced is copied to detect changes to it on next update
        self.referenced._set_value(self._referenced.copy(), force_write=True)

    @isasync
    def moveRel(self, shift):
        if not shift:
            return model.InstantaneousFuture()
        self._checkMoveRel(shift)
        raise NotImplementedError("Relative move on fixed positions axis not supported")

    @isasync
    def moveAbs(self, pos):
        """
        Move the actuator to the defined position in m for each axis given.
        pos dict(string-> float): name of the axis and position in m
        """
        if not pos:
            return model.InstantaneousFuture()
        self._checkMoveAbs(pos)
        pos = self._applyInversion(pos)
        f = self._executor.submit(self._doMoveAbs, pos)

        return f

    def _doMoveAbs(self, pos):
        p = pos[self._axis]
        p_cor = p + self._pos_shift
        prev_pos = self._dependency.position.value[self._axis]
        logging.debug("Moving axis %s to %g (corrected %g)", self._axis, p, p_cor)

        self._dependency.moveAbs(pos).result()

        # Check that it worked
        exp_spos = self._to_sensor[p]  # already checked that distance is there
        spos = self._sensor.position.value[self._axis_sensor]

        # If it didn't work, try 10x to move by an extra 10%
        retry = 0
        tot_shift = 0
        while spos != exp_spos:
            retry += 1
            if retry == 10:
                logging.warning("Failed to reach position %s (=%s) even after extra %s, still at %s",
                                p_cor, exp_spos, tot_shift, spos)
                raise IOError("Failed to reach position %s, sensor reports %s" % (p, spos))

            # Find 10% move
            if p_cor == prev_pos:
                # It was already at the "right" position => give up
                logging.warning("Actuator supposedly at position %s, but sensor reports %s",
                                exp_spos, spos)
                return
            shift = (p_cor - prev_pos) * 0.1

            logging.debug("Attempting to reach position %s (=%s) by moving an extra %s",
                          p_cor, exp_spos, shift)
            try:
                self._dependency.moveRel({self._axis: shift}).result()
            except Exception as ex:
                logging.warning("Failed to move further (%s)", ex)
                raise IOError("Failed to reach position %s, sensor reports %s" % (p, spos))

            tot_shift += shift
            spos = self._sensor.position.value[self._axis_sensor]

        # It worked, so save the shift
        self._pos_shift += tot_shift

    def _doReference(self, axes):
        # TODO:
        # 1. If the dependency is not referenced yet, reference it
        # 2. move to first position
        # 3. keep moving +10% until the sensor indicates a change
        # 4. do the same with every position
        # 5. store the updated position for each position.

        logging.debug("Referencing axis %s", self._axis)
        f = self._dependency.reference({self._axis})
        f.result()

    @isasync
    def reference(self, axes):
        if not axes:
            return model.InstantaneousFuture()
        self._checkReference(axes)

        f = self._executor.submit(self._doReference, axes)
        return f

    reference.__doc__ = model.Actuator.reference.__doc__

    def stop(self, axes=None):
        """
        stops the motion
        axes (iterable or None): list of axes to stop, or None if all should be stopped
        """
        self._dependency.stop(axes=axes)

    def terminate(self):
        if self._executor:
            self.stop()
            self._executor.shutdown(wait=True)
            self._executor = None

        self._dependency.position.unsubscribe(self._update_dep_position)
        self._sensor.position.unsubscribe(self._updatePosition)


class CombinedFixedPositionActuator(model.Actuator):
    """
    A generic actuator component which only allows moving to fixed positions
    defined by the user upon initialization. It is actually a wrapper to move
    two rotational actuators to fixed relative and absolute position (e.g.
    two polarization filters).
    """

    def __init__(self, name, role, dependencies, caxes_map, axis_name, positions, fallback,
                 atol=None, cycle=None, inverted=None, **kwargs):
        """
        name (string)
        role (string)
        dependencies (dict str -> actuator): axis name (in this actuator) -> actuator to be used for this axis
        caxes_map (list): axis names in the dependencies actuator
        axis_name (string): axis name in this actuator
        positions (dict str -> list with two entries): position combinations possible for dependencies axes
                                                       reported position name for axis  --> positions of each dependency axis
        fallback (str): position string reported when none of combination of dependency positions fits the dependencies
                        positions. Fallback position can be equal to one of the positions allowed. If fallback is not
                        equal to one of the positions allowed, it is not possible to request moving to this fallback
                        position.
        atol (list of (float or None)): absolute tolerance in the position of each dep. If None, set to 0.
        cycle (list of (float or None)): for each axis, the length of a full rotation until it reaches position 0 again.
                                        None is "infinity" (= no modulo)
        """
        if inverted:
            raise ValueError("Axes shouldn't be inverted")
        if len(dependencies) != 2:
            raise ValueError("CombinedFixedPositionActuator needs precisely two dependencies")
        if len(caxes_map) != 2:
            raise ValueError("CombinedFixedPositionActuator needs precisely two axis names for dependencies axes")
        if len(atol) != 2:
            raise ValueError("CombinedFixedPositionActuator needs list of "
                             "precisely two values for tolerance in position")
        for key, pos in positions.items():
            if not (len(pos) == 2 or isinstance(pos, str)):
                raise ValueError("Position %s needs to be of format list with exactly two entries. "
                                 "Got instead position %s." % (key, pos))

        self._axis_name = axis_name
        self._positions = positions
        self._atol = atol
        self._fallback = fallback
        self._cycle = cycle

        if cycle is None:
            self._cycle = (None,) * len(dependencies)
        if atol is None:
            self._atol = (0,) * len(dependencies)

        if len(self._cycle) != len(dependencies):
            raise ValueError("CombinedFixedPositionActuator has %s dependencies, so "
                             "need cycle being a list of same length." % len(dependencies))
        if len(atol) != len(dependencies):
            raise ValueError("CombinedFixedPositionActuator has %s dependencies, so "
                             "need atol being a list of same length." % len(dependencies))

        self._dependencies = [dependencies[r] for r in sorted(dependencies.keys())]
        # self._dependencies_futures = None
        # axis names of dependencies
        self._axes_map = [key for key in sorted(dependencies.keys())]
        # axis names of axes of dependencies
        self._caxes_map = caxes_map

        for i, (c, ac) in enumerate(zip(self._dependencies, self._caxes_map)):
            if ac not in c.axes:
                raise ValueError("Dependency %s has no axis named %s" % (c.name, ac))

            if hasattr(c.axes[ac], "range"):
                mn, mx = c.axes[ac].range
                for key, pos in self._positions.items():
                    if not mn <= pos[i] <= mx:
                        raise ValueError("Position %s with key %s is out of range for dependencies." % (pos[i], key))

            elif hasattr(c.axes[ac], "choices"):
                for pos in self._positions.values():
                    if pos[i] not in c.axes[ac].choices:
                        raise ValueError("Position %s is not in range of choices for dependencies." % pos[i])

        axes = {axis_name: model.Axis(choices=set(list(positions.keys()) + [fallback]))}

        # this set ._axes and ._dependencies
        model.Actuator.__init__(self, name, role, axes=axes, dependencies=dependencies, **kwargs)

        # will take care of executing axis move asynchronously
        self._executor = CancellableThreadPoolExecutor(max_workers=1)  # one task at a time

        # create position VA and subscribe to position
        self.position = model.VigilantAttribute({}, readonly=True)
        for c in self._dependencies:
            # subscribe to variable of each dependencies axis and call now with init=True
            c.position.subscribe(self._updatePosition, init=True)

        # check if dependencies axes can be referenced, create referenced VA, subscribe to referenced
        # list with entries True and/or False for dependencies
        self._dependencies_refd = [model.hasVA(dep, "referenced") and caxis in dep.referenced.value
                               for dep, caxis in zip(self._dependencies, self._caxes_map)]
        if any(self._dependencies_refd):
            # whether the axes are referenced
            self.referenced = model.VigilantAttribute({}, readonly=True)
            for c in itertools.compress(self._dependencies, self._dependencies_refd):
                # subscribe to variable of each dependencies axis and call now with init=True
                c.referenced.subscribe(self._updateReferenced, init=True)

    def _readPositionfromDependencies(self):
        """
        check if the dependencies axes positions correspond to any allowed combined axis position
        :return: position key in position dict for dependency axis
        """
        # get current dependencies positions
        pos_dependencies_cur = [c.position.value[ca] for c, ca in zip(self._dependencies, self._caxes_map)]

        # check whether current dependencies positions are in consistency with dependencies positions allowed
        for pos_key, pos_dependencies in self._positions.items():
            # cp: current dependencies position, tp: target dependencies position
            for cp, tp, atol, cyl in zip(pos_dependencies_cur, pos_dependencies, self._atol, self._cycle):
                # handle positions close to cycle and thus also close to zero
                # and within tolerance so util.almost_equal compare the correct values
                if cyl is None:
                    dist = abs(cp - tp)
                else:
                    dist = min((cp - tp) % cyl, (tp - cp) % cyl)
                if dist > atol:
                    break
            else:  # Never found a position _not_ different from actual dependencies axes positions => it's a match!
                return pos_key
        else:
            raise LookupError("Did not find any matching position. Reporting position %s." % pos_dependencies_cur)

    def _updatePosition(self, _=None):
        # _=None: optional argument, needed for VA calls
        """
        update the position VA
        """
        try:
            pos_key_matching = self._readPositionfromDependencies()

            # it's read-only, so we change it via _value
            self.position._set_value({self._axis_name: pos_key_matching}, force_write=True)
            logging.debug("reporting position %s", self.position.value)

        except LookupError:
            self.position._set_value({self._axis_name: self._fallback}, force_write=True)
            pos_dependencies = [c.position.value[ca] for c, ca in zip(self._dependencies, self._caxes_map)]
            logging.warning("Current position does not match any known position. Reporting position %s. "
                            "Positions of %s are %s." % (self.position.value, self._caxes_map, pos_dependencies))

    def _updateReferenced(self, _=None):
        """
        update the referenced VA
        """
        # Referenced if all the (referenceable) dependencies are referenced
        refd = all(c.referenced.value[ac] for c, ac, r in zip(self._dependencies, self._caxes_map, self._dependencies_refd)
                   if r)
        # .referenced is copied to detect changes to it on next update
        self.referenced._set_value({self._axis_name: refd}, force_write=True)
        logging.debug("Reporting referenced axis %s as %s.", self._axis_name, refd)

    @isasync
    def moveRel(self, shift):
        if not shift:
            return model.InstantaneousFuture()
        raise NotImplementedError("Relative move on combined fixed positions axis not supported")

    @isasync
    def moveAbs(self, pos):
        if not pos:
            return model.InstantaneousFuture()

        self._checkMoveAbs(pos)
        if pos[self._axis_name] == self._fallback:
            # raise error if user asks to move to fallback position
            raise ValueError("Not allowed to move to fallback position %s" % self._fallback)

        f = self._executor.submit(self._doMoveAbs, pos)

        # TODO: will be needed when cancel is implemented
        # self._cancelled = False
        # f = CancellableFuture()
        # f = self._executor.submitf(f, self._doMoveAbs, pos)
        # f.task_canceller = self._cancelMovement

        return f

    # TODO: needs to be implemented properly
    # def _cancelMovement(self, future):
    #     cancelled = False
    #     if self._dependencies_futures:
    #         if len(self._dependencies_futures) == 0:
    #             return True
    #         for f in self._dependencies_futures:
    #             cancelled = cancelled | f.cancel()
    #     self._cancelled = cancelled
    #     return cancelled

    def _doMoveAbs(self, pos):
        _pos = self._positions[pos[self._axis_name]]
        futures_dependencies = []
        # TODO: needed when cancel will be implemented
        # self._dependencies_futures = []

        try:
            # unsubscribe dependencies axes VAs while moving: during moving no reporting of the dependencies axes positions
            # is conducted as they would report positions, which do not match any position specified in positions.
            # The _updatePosition function would continuously report the fallback position.
            for c in self._dependencies:
                c.position.unsubscribe(self._updatePosition)

            for dep, ac, cp in zip(self._dependencies, self._caxes_map, _pos):
                f = dep.moveAbs({ac: cp})
                futures_dependencies.append(f)
                # TODO: needed when cancel will be implemented
                # self._dependencies_futures.append(f)

            # just wait for all futures to finish
            exceptions = []
            for f in futures_dependencies:
                try:
                    f.result()
                except Exception as ex:
                    logging.debug("Exception was raised by %s." % ex)
                    exceptions.append(ex)

            # TODO: needed when cancel will be implemented
            # for f in self._dependencies_futures:
            #     try:
            #         f.result()
            #     except CancelledError:
            #         logging.debug("Movement was cancelled.")
            #     except Exception as ex:
            #         logging.debug("Exception was raised by %s." % ex)
            #         exceptions.append(ex)
            # self._dependencies_futures = None

            self._updatePosition()

            if exceptions:
                raise exceptions[0]

        finally:
            # Resubscribe again after movement is done in finally to ensure that VAs are resubscribed also when an
            # unusual event has occurred (e.g. cancel).
            for c in self._dependencies:
                c.position.subscribe(self._updatePosition)

    @isasync
    def reference(self, axis):
        if not axis:
            return model.InstantaneousFuture()
        self._checkReference(axis)
        f = self._executor.submit(self._doReference, axis)

        return f

    # use doc string from model.actuator.reference
    reference.__doc__ = model.Actuator.reference.__doc__

    def _doReference(self, axes):
        try:
            # unsubscribe dependencies axes VAs while moving: during moving no reporting of the dependencies axes positions
            # is conducted as they would report positions, which do not match any position specified in positions.
            # The _updatePosition function would continuously report the fallback position.
            for c in self._dependencies:
                c.position.unsubscribe(self._updatePosition)

            # try:
            futures = []
            for dep, caxis in zip(self._dependencies, self._caxes_map):
                f = dep.reference({caxis})
                futures.append(f)

            # just wait for all futures to finish
            for f in futures:
                f.result()

            try:
                # check if referencing pos matches any position allowed
                self._readPositionfromDependencies()
            except LookupError:
                # If we just did referencing and ended up to an unsupported position,
                # move to closest supported position
                pos_dependencies = [c.position.value[ca] for c, ca in zip(self._dependencies, self._caxes_map)]
                pos_distances = {key: abs(pos_dependencies[0] - pos[0]) + abs(pos_dependencies[1] + pos[1])
                                 for key, pos in self._positions.items()}
                pos_key_closest = util.index_closest(0.0, pos_distances)
                self._doMoveAbs({self._axis_name: pos_key_closest})
        finally:
            # Resubscribe again after movement is done in finally to ensure that VAs are resubscribed also when an
            # unusual event has occurred (e.g. cancel).
            for c in self._dependencies:
                c.position.subscribe(self._updatePosition)

    def stop(self, axes=None):
        """
        stops the motion
        axes (iterable or None): list of axes to stop, or None if all should be stopped
        """
        # Empty the queue for the given axes
        if self._executor:
            self._executor.cancel()

        if axes is not None and self._axis_name not in axes:
            logging.warning("Trying to stop without any existing axis")
            return

        threads = []
        for dep, ac in zip(self._dependencies, self._caxes_map):
            # it's synchronous, but we want to stop all of them as soon as possible
            thread = threading.Thread(name="Stopping axis", target=dep.stop, args=({ac},))
            thread.start()
            threads.append(thread)

        # wait for completion
        for thread in threads:
            thread.join(1)
            if thread.is_alive():
                logging.warning("Stopping dependency actuator of '%s' is taking more than 1s", self.name)

    def terminate(self):
        if self._executor:
            self.stop()
            self._executor.shutdown()
            self._executor = None

        for c in self._dependencies:
            c.position.unsubscribe(self._updatePosition)
        for c in itertools.compress(self._dependencies, self._dependencies_refd):
            c.referenced.unsubscribe(self._updateReferenced)


class RotationActuator(model.Actuator):
    """
    Wrapper component for a single actuator axis which does complete rotations but reports it as a (almost) infinite
    linear axis. It ensures that a move is done by going via the fastest direction, that referencing is done regularly
    in order to avoid error accumulation in the position and converts the reported position to a limited range
    0 -> cycle. It also supports to pass an offset to the position conversion, via the MD_POS_COR metadata.
    """

    def __init__(self, name, role, dependencies, axis_name, cycle=2 * math.pi,
                 ref_start=None, inverted=None, **kwargs):
        """
        name (string)
        role (string)
        dependencies (dict str -> actuator): axis name (in this actuator) -> actuator to be used for this axis
        axis_name (str): axis name in the dependency actuator
        cycle (float): 0 < float. Default value = 2pi.
        ref_start (float or None): Value usually chosen close to reference switch from where to start referencing.
                                    Used to optimize runtime for referencing.
                                    If None, value will be 5% of value of cycle.
        """
        if inverted:
            raise ValueError("Axes shouldn't be inverted")

        if len(dependencies) != 1:
            raise ValueError("RotationActuator needs precisely one dependency")

        self._cycle = cycle
        # counter to check when current position has overrun cycle
        # and is close to zero again (pos and neg direction)
        self._move_sum = 0
        # check when a specified number of rotations was performed
        self._move_num_total = 0
        axis, dep = list(dependencies.items())[0]
        self._axis = axis
        self._dependency = dep
        self._dep_future = None
        self._caxis = axis_name

        # just an offset to reference switch position using the shortest move
        if ref_start is None:
            ref_start = cycle * 0.05
        self._ref_start = ref_start

        # Executor used to reference and move to nearest position
        self._executor = CancellableThreadPoolExecutor(max_workers=1)  # one task at a time

        if not isinstance(dep, model.ComponentBase):
            raise ValueError("Dependency %s is not a component." % (dep,))
        if not hasattr(dep, "axes") or not isinstance(dep.axes, dict):
            raise ValueError("Dependency %s is not an actuator." % dep.name)
        if not self._cycle >= 0:
            raise ValueError("Cycle needs to be a positive number. Got value %s." % self._cycle)
        if not 0 <= self._ref_start <= self._cycle:
            raise ValueError("Reference start needs to be a positive number within range of cycle. "
                             "Got value %s." % self._ref_start)

        ac = dep.axes[axis_name]
        # dict {axis_name --> driver}
        axes = {axis: model.Axis(range=(0, self._cycle), unit=ac.unit)}  # TODO: allow the user to override the unit?

        model.Actuator.__init__(self, name, role, axes=axes, dependencies=dependencies, **kwargs)

        # set offset due to mounting of components (float)
        self._metadata[model.MD_POS_COR] = 0.

        self.position = model.VigilantAttribute({}, readonly=True)

        logging.debug("Subscribing to position of dependency %s", dep.name)
        # subscribe to variable and call now with init=True
        dep.position.subscribe(self._updatePosition, init=True)

        if model.hasVA(dep, "referenced") and axis_name in dep.referenced.value:
            self._referenced = {}
            self.referenced = model.VigilantAttribute(self._referenced.copy(), readonly=True)
            # subscribe to variable and call now with init=True
            dep.referenced.subscribe(self._updateReferenced, init=True)

            # If the axis can be referenced => do it now (and move to a known position)
            # In case of cyclic move always reference
            if not self.referenced.value[axis]:
                # The initialisation will not fail if the referencing fails
                f = self.reference({axis})
                f.add_done_callback(self._on_referenced)

    def updateMetadata(self, md):
        for key, value in md.items():
            if isinstance(value, numbers.Real) and (0 <= abs(value) <= self._cycle/2):
                super(RotationActuator, self).updateMetadata(md)
                self._updatePosition()
                logging.debug("reporting metadata entry %s with value %s." % (value, key))
            else:
                logging.error("value %s for metadata entry %s is not allowed. "
                                  "Value should be in range -%s/2 and +%s/2." % (value, key, self._cycle, self._cycle))
                raise ValueError("value %s for metadata entry %s is not allowed." % (value, key))

    def _on_referenced(self, future):
        try:
            future.result()
        except Exception as e:
            self._dependency.stop({self._caxis})  # prevent any move queued
            self.state._set_value(e, force_write=True)
            logging.exception(e)

    def _updatePosition(self, _=None):
        """
        update the position VA
        """
        p = self._dependency.position.value[self._caxis] - self._metadata[model.MD_POS_COR]
        p %= self._cycle
        pos = {self._axis: p}
        logging.debug("reporting position %s", pos)
        self.position._set_value(pos, force_write=True)

    def _updateReferenced(self, _=None):
        """
        update the referenced VA
        """
        # get dependency VA value, update dict value
        self._referenced[self._axis] = self._dependency.referenced.value[self._caxis]
        # ._referenced is copied to detect changes to it on next update
        # update VA value
        self.referenced._set_value(self._referenced.copy(), force_write=True)


    @isasync
    def moveRel(self, shift):
        """
        Move the rotation actuator by a defined shift.
        shift dict(string-> float): name of the axis and shift
        """
        if not shift:
            return model.InstantaneousFuture()
        self._checkMoveRel(shift)
        f = self._executor.submit(self._doMoveRel, shift)

        # TODO: needed when cancel will be implemented
        # f = CancellableFuture()
        # f = self._executor.submitf(f, self._doMoveRel, shift)
        # f.task_canceller = self._cancelMovement

        return f

    def _doMoveRel(self, shift):
        """
        shift dict(string-> float): name of the axis and shift
        """
        cur_pos = self._dependency.position.value[self._caxis] - self._metadata[model.MD_POS_COR]
        pos = cur_pos + shift[self._axis]
        self._doMoveAbs({self._axis: pos})

    @isasync
    def moveAbs(self, pos):
        """
        Move the actuator to the defined position for a given axis.
        pos dict(string-> float): name of the axis and position
        """
        if not pos:
            return model.InstantaneousFuture()
        self._checkMoveAbs(pos)
        f = self._executor.submit(self._doMoveAbs, pos)

        # TODO: needed when cancel will be implemented
        # f = CancellableFuture()
        # f = self._executor.submitf(f, self._doMoveAbs, pos)
        # f.task_canceller = self._cancelMovement

        return f

    def _doMoveAbs(self, pos):
        """
        pos dict(string-> float): name of the axis and position
        """
        target_pos = pos[self._axis]
        # correct distance for physical offset due to mounting
        target_pos += self._metadata[model.MD_POS_COR]
        logging.debug("Moving axis %s (-> %s) to %g", self._axis, self._caxis, target_pos)

        self._move_num_total += 1

        # calc move needed to reach requested pos
        move, cur_pos = self._findShortestMove(target_pos)

        # Check that the move passes by the reference switch by detecting whether
        # the current and final position are not in the same multiple of cycle.
        # "Linear" view of the axis (c = cycle)
        #    -2c       -1c        0         c         2c        3c
        # ----|---------|---------|---------|---------|---------|---
        #         -2            -1      0        1        2
        final_pos = cur_pos + move
        pass_ref = (cur_pos // self._cycle) != (final_pos // self._cycle)

        # do referencing after i=5 moves or when passing the reference switch anyways
        if pass_ref or self._move_num_total >= 5:
            # Move to pos close to ref switch
            move, cur_pos = self._findShortestMove(self._ref_start)
                
            self._dependency.moveRel({self._caxis: move}).result()
            self._dependency.reference({self._caxis}).result()

            # now calc how to move to the actual position requested
            move, cur_pos = self._findShortestMove(target_pos)
                
            self._move_num_total = 0

        _dep_future = self._dependency.moveRel({self._caxis: move})
        _dep_future.result()

        # TODO: needed when cancel will be implemented
        # self._dep_future = self._dependency.moveRel({self._caxis: move})
        # self._dep_future.result()
        # self._dep_future = None

    def _findShortestMove(self, target_pos):
        """Find the closest way to move through in order to optimize for runtime."""

        cur_pos = self._dependency.position.value[self._caxis]
        vector = target_pos - cur_pos
        # mod1 and mod2 should be always positive as self._cycle should be positive
        mod1 = vector % self._cycle
        mod2 = -vector % self._cycle

        if mod1 < mod2:
            return mod1, cur_pos
        else:
            return -mod2, cur_pos

    # TODO: need a proper implementation
    # def _cancelMovement(self, future):
        # if self._dep_future:
        #     return self._dep_future.cancel()
        # return False

    def _doReference(self, axes):
        logging.debug("Referencing axis %s (-> %s)", self._axis, self._caxis)
        f = self._dependency.reference({self._caxis})
        f.result()

    @isasync
    def reference(self, axes):
        if not axes:
            return model.InstantaneousFuture()
        self._checkReference(axes)

        f = self._executor.submit(self._doReference, axes)
        return f

    reference.__doc__ = model.Actuator.reference.__doc__

    def stop(self, axes=None):
        """
        stops the motion
        axes (iterable or None): list of axes to stop, or None if all should be stopped
        """
        # Empty the queue for the given axes
        if self._executor:
            self._executor.cancel()

        if axes is not None:
            axes = set()
            if self._axis in axes:
                axes.add(self._caxis)

        self._dependency.stop(axes=axes)

    def terminate(self):
        if self._executor:
            self.stop()
            self._executor.shutdown(wait=True)
            self._executor = None

        self._dependency.position.unsubscribe(self._updatePosition)
        if hasattr(self, "referenced"):
            self._dependency.referenced.subscribe(self._updateReferenced)
