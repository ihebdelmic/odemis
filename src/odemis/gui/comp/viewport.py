# -*- coding: utf-8 -*-
"""
Created on 8 Feb 2012

:author: Éric Piel
:copyright: © 2012 Éric Piel, Delmic

.. license::

    This file is part of Odemis.

    Odemis is free software: you can redistribute it and/or modify it under the
    terms of the GNU General Public License version 2 as published by the Free
    Software Foundation.

    Odemis is distributed in the hope that it will be useful, but WITHOUT ANY
    WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
    FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
    details.

    You should have received a copy of the GNU General Public License along with
    Odemis. If not, see http://www.gnu.org/licenses/.

"""

from __future__ import division

import collections
import logging
from odemis import gui, model
from odemis.gui.comp import miccanvas
from odemis.gui.comp.canvas import CAN_MOVE, CAN_FOCUS
from odemis.gui.comp.legend import InfoLegend, AxisLegend
from odemis.gui.img.data import getico_blending_goalBitmap
from odemis.acq.stream import OPTICAL_STREAMS, EM_STREAMS
from odemis.gui.util import call_after
from odemis.util import units
import wx


class ViewPort(wx.Panel):

    # Default classes for the canvas and the legend. These may be overridden
    # in subclasses
    canvas_class = miccanvas.DblMicroscopeCanvas
    legend_class = None

    def __init__(self, *args, **kwargs):
        """Note: The MicroscopeViewport is not fully initialised until setView()
        has been called.
        """
        wx.Panel.__init__(self, *args, **kwargs)

        self._microscope_view = None  # model.MicroscopeView
        self._tab_data_model = None # model.MicroscopyGUIData

        # Keep track of this panel's pseudo focus
        self._has_focus = False

        font = wx.Font(8, wx.FONTFAMILY_DEFAULT,
                          wx.FONTSTYLE_NORMAL,
                          wx.FONTWEIGHT_NORMAL)
        self.SetFont(font)
        self.SetBackgroundColour("#1A1A1A")
        self.SetForegroundColour("#BBBBBB")

        # main widget
        self.canvas = self.canvas_class(self)

        # Put all together (canvas + legend)

        self.legend = None

        mainSizer = wx.BoxSizer(wx.VERTICAL)

        if self.legend_class is not None:
            if isinstance(self.legend_class, collections.Iterable):
                # FIXME: don't use a list, it will confuse everything.
                # => Just use legend_bottom, legend_left
                self.legend = [self.legend_class[0](self),
                               self.legend_class[1](self)]

                self.legend[1].orientation = self.legend[1].VERTICAL
                self.legend[1].MinSize = (40, -1)

                grid_sizer = wx.GridBagSizer()

                grid_sizer.Add(self.canvas, pos=(0, 1), flag=wx.EXPAND)

                grid_sizer.Add(self.legend[0], pos=(1, 1), flag=wx.EXPAND)
                grid_sizer.Add(self.legend[1], pos=(0, 0), flag=wx.EXPAND)

                filler = wx.Panel(self)
                filler.SetBackgroundColour("#1A1A1A")
                grid_sizer.Add(filler, pos=(1, 0), flag=wx.EXPAND)

                grid_sizer.AddGrowableRow(0, 1)
                grid_sizer.AddGrowableCol(1, 1)
                # grid_sizer.RemoveGrowableCol(0)


                # Focus the view when a child element is clicked
                for lp in self.legend:
                    lp.Bind(wx.EVT_LEFT_DOWN, self.OnChildFocus)

                mainSizer.Add(grid_sizer, 1,
                        border=2, flag=wx.EXPAND | wx.ALL)
            else:
                mainSizer.Add(self.canvas, 1,
                    border=2, flag=wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT)
                # It's made of multiple controls positioned via sizers
                # TODO: allow the user to pick which information is displayed
                # in the legend
                # pylint: disable=E1102, E1103
                self.legend = self.legend_class(self)
                self.legend.Bind(wx.EVT_LEFT_DOWN, self.OnChildFocus)

                mainSizer.Add(self.legend, 0,
                        border=2, flag=wx.EXPAND|wx.BOTTOM|wx.LEFT|wx.RIGHT)
        else:
            mainSizer.Add(self.canvas, 1,
                border=2, flag=wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT)

        self.SetSizerAndFit(mainSizer)
        mainSizer.Fit(self)
        self.SetAutoLayout(True)

        self.Bind(wx.EVT_CHILD_FOCUS, self.OnChildFocus)
        self.Bind(wx.EVT_SIZE, self.OnSize)

    def __str__(self):
        return "{0} {2} {1}".format(
            self.__class__.__name__,
            self._microscope_view.name.value if self._microscope_view else "",
            id(self))

    __repr__ = __str__

    @property
    def microscope_view(self):
        return self._microscope_view

    def setView(self, microscope_view, tab_data):
        raise NotImplementedError

    ################################################
    ## Panel control
    ################################################

    def ShowLegend(self, show):
        """ Show or hide the merge slider """
        self.legend.Show(show)  #pylint: disable=E1103

    def HasFocus(self, *args, **kwargs):
        return self._has_focus == True

    def SetFocus(self, focus):   #pylint: disable=W0221
        """ Set the focus on the viewport according to the focus parameter.
        focus:  A boolean value.
        """
        logging.debug(["Removing focus from %s", "Setting focus to %s"][focus], id(self))

        self._has_focus = focus
        if focus:
            self.SetBackgroundColour(gui.BORDER_COLOUR_FOCUS)
        else:
            self.SetBackgroundColour(gui.BORDER_COLOUR_UNFOCUS)

    ################################################
    ## GUI Event handling
    ################################################

    def OnChildFocus(self, evt):
        """ When one of it's child widgets is clicked, this viewport should be
        considered as having the focus.
        """
        if self._microscope_view and self._tab_data_model:
            # This will take care of doing everything necessary
            # Remember, the notify method of the vigilant attribute will
            # only fire if the values changes.
            self._tab_data_model.focussedView.value = self._microscope_view

        evt.Skip()

    def OnSize(self, evt):
        evt.Skip() # processed also by the parent


class MicroscopeViewport(ViewPort):
    """ A panel that shows a microscope view and its legend below it.

    This is a generic class, that should be inherited by more specific classes.
    """

    legend_class = InfoLegend

    def __init__(self, *args, **kwargs):
        """Note: The MicroscopeViewport is not fully initialised until setView()
        has been called.
        """
        # Call parent constructor at the end, because it needs the legen panel
        ViewPort.__init__(self, *args, **kwargs)

        # Bind on EVT_SLIDER to update even while the user is moving
        self.legend.Bind(wx.EVT_LEFT_UP, self.OnSlider)
        self.legend.Bind(wx.EVT_SLIDER, self.OnSlider)


    def setView(self, microscope_view, tab_data):
        """
        Set the microscope view that this viewport is displaying/representing
        *Important*: Should be called only once, at initialisation.

        :param microscope_view:(model.MicroscopeView)
        :param tab_data: (model.MicroscopyGUIData)
        """

        # This is a kind of a kludge, as it'd be best to have the viewport
        # created after the microscope view, but they are created independently
        # via XRC.
        assert(self._microscope_view is None)

        # import traceback
        # traceback.print_stack()

        self._microscope_view = microscope_view
        self._tab_data_model = tab_data

        # TODO: Center to current view position, with current mpp
        microscope_view.mpp.subscribe(self._onMPP, init=True)

        # set/subscribe merge ratio
        microscope_view.merge_ratio.subscribe(self._onMergeRatio, init=True)

        # subscribe to image, to update legend on streamtree/image change
        microscope_view.lastUpdate.subscribe(self._onImageUpdate, init=True)

        # canvas handles also directly some of the view properties
        self.canvas.setView(microscope_view, tab_data)


    ################################################
    ## Panel control
    ################################################

    def ShowMergeSlider(self, show):
        """ Show or hide the merge slider """
        self.legend.bmp_slider_left.Show(show)
        self.legend.merge_slider.Show(show)
        self.legend.bmp_slider_right.Show(show)

    def UpdateHFWLabel(self):
        """ Physical width of the display"""
        if not self._microscope_view:
            return
        hfw = self._microscope_view.mpp.value * self.GetClientSize()[0]
        hfw = units.round_significant(hfw, 4)
        label = u"HFW: %s" % units.readable_str(hfw, "m", sig=3)
        self.legend.set_hfw_label(label)

    def UpdateMagnification(self):
        # TODO: shall we use the real density of the screen?
        # We could use real density but how much important is it?
        mppScreen = 0.00025 # 0.25 mm/px
        label = u"Mag: "

        # three possibilities:
        # * no image => total mag (using current mpp)
        # * all images have same mpp => mag instrument * mag digital
        # * >1 mpp => total mag

        # get all the mpps
        mpps = set()
        for im in self._microscope_view.stream_tree.getImages():
            try:
                mpps.add(im.metadata[model.MD_PIXEL_SIZE][0])
            except KeyError:
                pass

        if len(mpps) == 1:
            # two magnifications
            im_mpp = mpps.pop()
            magIm = mppScreen / im_mpp # as if 1 im.px == 1 sc.px
            if magIm >= 1:
                label += u"×" + units.readable_str(units.round_significant(magIm, 3))
            else:
                label += u"÷" + units.readable_str(units.round_significant(1.0 / magIm, 3))
            magDig = im_mpp / self._microscope_view.mpp.value
            if magDig >= 1:
                label += u" ×" + units.readable_str(units.round_significant(magDig, 3))
            else:
                label += u" ÷" + units.readable_str(units.round_significant(1.0 / magDig, 3))
        else:
            # one magnification
            mag = mppScreen / self._microscope_view.mpp.value
            if mag >= 1:
                label += u"×" + units.readable_str(units.round_significant(mag, 3))
            else:
                label += u"÷" + units.readable_str(units.round_significant(1.0 / mag, 3))

        self.legend.set_mag_label(label)

    ################################################
    ## VA handling
    ################################################

    @call_after
    def _onMergeRatio(self, val):
        # round is important because int can cause unstable value
        # int(0.58*100) = 57
        self.legend.merge_slider.SetValue(round(val * 100))

    @call_after
    def _onMPP(self, mpp):
        self.legend.scale_win.SetMPP(mpp)
        self.UpdateHFWLabel()
        self.UpdateMagnification()
        # the MicroscopeView will send an event that the view has to be redrawn

    def _checkMergeSliderDisplay(self):
        """
        Update the MergeSlider display and icons depending on the state
        """
        # MergeSlider is displayed if:
        # * Root operator of StreamTree accepts merge argument
        # * (and) Root operator of StreamTree has >= 2 images
        if ("merge" in self._microscope_view.stream_tree.kwargs and
            len(self._microscope_view.stream_tree) >= 2):

            # How is the order guaranteed? (Left vs Right)
            sc = self._microscope_view.stream_tree[0]
            self.legend.set_stream_type(wx.LEFT, sc.__class__)

            sc = self._microscope_view.stream_tree[1]
            self.legend.set_stream_type(wx.RIGHT, sc.__class__)

            self.ShowMergeSlider(True)
        else:
            self.ShowMergeSlider(False)


    @call_after
    def _onImageUpdate(self, timestamp):
        self._checkMergeSliderDisplay()

        # magnification might have changed (eg, image with different binning)
        self.UpdateMagnification()

    ################################################
    ## GUI Event handling
    ################################################

    def OnSlider(self, evt):
        """
        Merge ratio slider
        """
        if self._microscope_view is None:
            return

        val = self.legend.merge_slider.GetValue() / 100
        self._microscope_view.merge_ratio.value = val
        evt.Skip()

    def OnSize(self, evt):
        evt.Skip() # processed also by the parent
        self.UpdateHFWLabel()

    def OnSliderIconClick(self, evt):
        evt.Skip()

        if self._microscope_view is None:
            return

        if(evt.GetEventObject() == self.legend.bmp_slider_left):
            self.legend.merge_slider.set_to_min_val()
        else:
            self.legend.merge_slider.set_to_max_val()

        val = self.legend.merge_slider.GetValue() / 100
        self._microscope_view.merge_ratio.value = val
        evt.Skip()

    ## END Event handling

class SecomViewport(MicroscopeViewport):

    canvas_class = miccanvas.SecomCanvas

    def __init__(self, *args, **kwargs):
        super(SecomViewport, self).__init__(*args, **kwargs)

    def setView(self, microscope_view, tab_data):
        super(SecomViewport, self).setView(microscope_view, tab_data)
        self._orig_abilities = self.canvas.abilities & set([CAN_MOVE, CAN_FOCUS])
        self._microscope_view.stream_tree.should_update.subscribe(
                                                        self.hide_pause,
                                                        init=True
        )

    def hide_pause(self, is_playing):
        #pylint: disable=E1101
        self.canvas.icon_overlay.hide_pause(is_playing)
        if hasattr(self._microscope_view, "stage_pos"):
            # disable/enable move and focus change
            if is_playing:
                self.canvas.abilities |= self._orig_abilities
            else:
                self.canvas.abilities -= set([CAN_MOVE, CAN_FOCUS])

    def _checkMergeSliderDisplay(self):
        # Overridden to avoid displaying merge slide if only SEM or only Optical
        # display iif both EM and OPT streams
        streams = self._microscope_view.getStreams()
        has_opt = any(isinstance(s, OPTICAL_STREAMS) for s in streams)
        has_em = any(isinstance(s, EM_STREAMS) for s in streams)

        if (has_opt and has_em):
            self.ShowMergeSlider(True)
        else:
            self.ShowMergeSlider(False)

class SparcAcquisitionViewport(MicroscopeViewport):

    canvas_class = miccanvas.SparcAcquiCanvas

    def __init__(self, *args, **kwargs):
        super(SparcAcquisitionViewport, self).__init__(*args, **kwargs)

class SparcAlignViewport(MicroscopeViewport):
    """
    Very simple viewport with no zoom or move allowed
    """
    canvas_class = miccanvas.SparcAlignCanvas

    def __init__(self, *args, **kwargs):
        super(SparcAlignViewport, self).__init__(*args, **kwargs)
        # TODO: should be done on the fly by _checkMergeSliderDisplay()
        # change SEM icon to Goal
        # pylint: disable=E1103
        self.legend.bmp_slider_right.SetBitmap(getico_blending_goalBitmap())


class PlotViewport(ViewPort):
    """ Class for displaying plotted data """

    # Default class
    canvas_class = miccanvas.ZeroDimensionalPlotCanvas
    legend_class = (AxisLegend, AxisLegend)

    def __init__(self, *args, **kwargs):
        ViewPort.__init__(self, *args, **kwargs)
        # We need a local reference to the spectrum stream, because if we rely
        # on the reference within the MicorscopeView, it might be replaced
        # before we get an explicit chance to unsubscribe event handlers
        self.spectrum_stream = None

    def clear(self):
        #pylint: disable=E1103, E1101
        self.canvas.clear()
        for l in self.legend:
            l.clear()
        self.Refresh()

    def OnSize(self, evt):
        evt.Skip() # processed also by the parent

    @property
    def microscope_view(self):
        return self._microscope_view

    def connect_stream(self, should_update=None):
        """ This method will connect this ViewPort to the Spectrum Stream so it
        it can react to spectrum pixel selection.
        """
        if should_update:
            ss = self.microscope_view.stream_tree.spectrum_streams

            # There should be exactly one Spectrum stream. In the future there
            # might be scenarios where there are more than one.
            if len(ss) != 1:
                raise ValueError("Unexpected number of Spectrum Streams found!")

            self.spectrum_stream = ss[0]
            self.spectrum_stream.selected_pixel.subscribe(self._on_pixel_select)

    def _on_pixel_select(self, pixel):
        """ Pixel selection event handler """
        data = self.spectrum_stream.get_pixel_spectrum()
        domain = self.spectrum_stream.get_spectrum_range()
        unit_x = self.spectrum_stream.spectrumBandwidth.unit
        self.legend[0].unit = unit_x
        self.canvas.set_1d_data(domain, data, unit_x)  #pylint: disable=E1101
        for l in self.legend:
            l.clear()

    def setView(self, microscope_view, tab_data):
        """
        Set the microscope view that this viewport is displaying/representing
        *Important*: Should be called only once, at initialisation.

        :param microscope_view:(model.View)
        :param tab_data: (model.MicroscopyGUIData)

        TODO: rename `microscope_view`, since this parameter is a regular view
        """

        # This is a kind of a kludge, as it'd be best to have the viewport
        # created after the microscope view, but they are created independently
        # via XRC.
        assert(self._microscope_view is None)

        # import traceback
        # traceback.print_stack()

        self._microscope_view = microscope_view
        self._tab_data_model = tab_data

        # canvas handles also directly some of the view properties
        self.canvas.setView(microscope_view, tab_data)

        # Keep an eye on the stream tree, so we can (re)connect when it changes
        microscope_view.stream_tree.should_update.subscribe(
                                                        self.connect_stream)

class AngularResolvedViewport(ViewPort):

    # Default class
    canvas_class = miccanvas.AngularResolvedCanvas
    legend_class = None

    def __init__(self, *args, **kwargs):
        ViewPort.__init__(self, *args, **kwargs)


    def setView(self, microscope_view, tab_data):
        """
        """

        # This is a kind of a kludge, as it'd be best to have the viewport
        # created after the microscope view, but they are created independently
        # via XRC.
        assert(self._microscope_view is None)

        # import traceback
        # traceback.print_stack()

        self._microscope_view = microscope_view
        self._tab_data_model = tab_data

        # canvas handles also directly some of the view properties
        self.canvas.setView(microscope_view, tab_data)

