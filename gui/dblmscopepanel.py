#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
Created on 8 Feb 2012

@author: Éric Piel

Copyright © 2012 Éric Piel, Delmic

This file is part of Delmic Acquisition Software.

Delmic Acquisition Software is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 2 of the License, or (at your option) any later version.

Delmic Acquisition Software is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with Delmic Acquisition Software. If not, see http://www.gnu.org/licenses/.
'''

import sys

import units
import wx

from dblmscopecanvas import DblMicroscopeCanvas
from dblmscopeviewmodel import DblMscopeViewModel
from odemis.gui.comp.scalewindow import ScaleWindow
from odemis.gui.comp.slider import CustomSlider
from odemis.gui.instrmodel import InstrumentalImage
from odemis.gui.img.data import getico_blending_optBitmap, \
    getico_blending_semBitmap

class DblMicroscopePanel(wx.Panel):
    """
    A draggable, flicker-free window class adapted to show pictures of two
    microscope simultaneously.

    """
    def __init__(self, *args, **kwargs):
        wx.Panel.__init__(self, *args, **kwargs)

        try:
            self.secom_model = wx.GetApp().secom_model
        except AttributeError:
            msg = "Could not find SECOM model"
            wx.MessageBox(msg,
                          "Application error",
                          style=wx.OK|wx.ICON_ERROR)
            sys.exit(1)

        self.viewmodel = DblMscopeViewModel()

        self.canvas = DblMicroscopeCanvas(self)

        font = wx.Font(8, wx.FONTFAMILY_DEFAULT,
                          wx.FONTSTYLE_NORMAL,
                          wx.FONTWEIGHT_NORMAL)
        self.SetFont(font)
        self.SetBackgroundColour("#1A1A1A")
        self.SetForegroundColour("#BBBBBB")

        ###################################
        # Standard legend widgets
        ###################################

        ##### Scale window

        self.scaleDisplay = ScaleWindow(self)
        self.scaleDisplay.SetFont(font)


        #### Values`

        self.magni_label = wx.StaticText(self, wx.ID_ANY, "Mag: 10x 10x")
        self.volta_label = wx.StaticText(self, wx.ID_ANY, "Volt: 66 kV")
        self.dwell_label = wx.StaticText(self, wx.ID_ANY, "Dwell: 666 μs")

        # Merge icons will be grabbed from gui.img.data
        ##### Merge slider

        self.mergeSlider = CustomSlider(self,
                    wx.ID_ANY,
                    50,
                    (0, 100),
                    size=(100, 16),
                    style=wx.SL_HORIZONTAL | wx.SL_AUTOTICKS | wx.SL_TICKS)
        self.mergeSlider.SetBackgroundColour(self.GetBackgroundColour())
        self.mergeSlider.SetForegroundColour("#4d4d4d")
        #self.mergeSlider.SetLineSize(50)

        self.bmpIconOpt = wx.StaticBitmap(self, wx.ID_ANY, getico_blending_optBitmap())
        self.bmpIconSem = wx.StaticBitmap(self, wx.ID_ANY, getico_blending_semBitmap())

        self.mergeSlider.Bind(wx.EVT_SLIDER, self.OnSlider)
        self.viewmodel.merge_ratio.subscribe(self.avOnMergeRatio, True) #pylint: disable=E1101



        ###################################
        # Optional legend widgets
        ###################################

        self.hfwDisplay = wx.StaticText(self) # Horizontal Full Width


        ###################################
        # Size composition
        ###################################

        #  Scale
        # +-------
        #  HFW text

        leftColSizer = wx.BoxSizer(wx.VERTICAL)
        leftColSizer.Add(self.scaleDisplay, flag=wx.EXPAND)
        leftColSizer.Add(self.hfwDisplay)

        #  | Value label | Value label | Value label |
        # +-------
        #  (?????) empty for now

        labelSizer = wx.BoxSizer(wx.HORIZONTAL)
        labelSizer.Add(self.magni_label, flag=wx.RIGHT, border=5)
        labelSizer.Add(self.volta_label, flag=wx.RIGHT, border=5)
        labelSizer.Add(self.dwell_label, flag=wx.RIGHT, border=5)

        midColSizer = wx.BoxSizer(wx.VERTICAL)
        midColSizer.Add(labelSizer)

        #  | Icon | Slider | Icon |
        # +-------
        #  (?????) empty for now

        sliderSizer = wx.BoxSizer(wx.HORIZONTAL)

        sliderSizer.Add(self.bmpIconOpt, flag=wx.ALL, border=5)
        sliderSizer.Add(self.mergeSlider, flag=wx.ALL, border=5)
        sliderSizer.Add(self.bmpIconSem, flag=wx.ALL, border=5)

        rightColSizer = wx.BoxSizer(wx.VERTICAL)
        rightColSizer.Add(sliderSizer)

        # leftColSizer | midColSizer | rightColSizer
        legendSizer = wx.BoxSizer(wx.HORIZONTAL)

        legendSizer.Add(leftColSizer, 1, flag=wx.EXPAND|wx.ALL, border=5)
        legendSizer.Add(midColSizer, 2, flag=wx.EXPAND|wx.ALL, border=5)
        legendSizer.Add(rightColSizer, 1, flag=wx.EXPAND|wx.ALL, border=5)

        #  Canvas
        # +------
        #  Legend Sizer

        mainSizer = wx.BoxSizer(wx.VERTICAL)

        mainSizer.Add(self.canvas, 1, flag=wx.EXPAND)
        mainSizer.Add(legendSizer, 0, wx.EXPAND) # 0 = fixed minimal size


        # Control for the selection before AddView(), which needs them
        #self.viewComboLeft = wx.ComboBox(self, style=wx.CB_READONLY, size=(140, -1))
        #self.viewComboRight = wx.ComboBox(self, style=wx.CB_READONLY, size=(140, -1))

        #self.Bind(wx.EVT_COMBOBOX, self.OnComboLeft, self.viewComboLeft)
        #self.Bind(wx.EVT_COMBOBOX, self.OnComboRight, self.viewComboRight)






        #                                      mainSizer
        #                    Canvas
        # legendSizer\/
        #|------scaleSizer---|---------------------imageSizer-----|
        #|                   l      imageSizerTop                 |
        #|-------------------l------------------------------------|
        #|                   l     imageSizerBottom               |
        #|                   l imageSizerBLeft l imageSizerBRight |
        #|-------------------|------------------------------------|



        self.viewmodel.mpp.subscribe(self.avOnMPP, True)

        # imageSizer = wx.BoxSizer(wx.VERTICAL)
        # imageSizerTop = wx.BoxSizer(wx.HORIZONTAL)
        # imageSizerBottom = wx.BoxSizer(wx.HORIZONTAL)
        # imageSizer.Add(imageSizerTop, 1, wx.ALIGN_CENTER|wx.EXPAND)
        # imageSizer.Add(imageSizerBottom, 1, wx.ALIGN_CENTER|wx.EXPAND)

        #imageSizerTop.Add(self.viewComboLeft, 0, wx.ALIGN_CENTER)
        #imageSizerTop.AddStretchSpacer()
        # imageSizerTop.Add(self.mergeSlider, 2, wx.ALIGN_CENTER|wx.LEFT|wx.RIGHT, 3)
        #imageSizerTop.AddStretchSpacer()
        #imageSizerTop.Add(self.viewComboRight, 0, wx.ALIGN_CENTER)


        # self.imageSizerBLeft = wx.BoxSizer(wx.HORIZONTAL)
        # self.imageSizerBRight = wx.BoxSizer(wx.HORIZONTAL)
        # because the statictexts cannot be vertically centered
        # sizervbl = wx.BoxSizer(wx.VERTICAL)
        # sizervbl.AddStretchSpacer()
        # sizervbl.Add(self.imageSizerBLeft, 0, wx.ALIGN_CENTER|wx.EXPAND)
        # sizervbl.AddStretchSpacer()
        # sizervbr = wx.BoxSizer(wx.VERTICAL)
        # sizervbr.AddStretchSpacer()
        # sizervbr.Add(self.imageSizerBRight, 0, wx.ALIGN_CENTER|wx.EXPAND)
        # sizervbr.AddStretchSpacer()
        # imageSizerBottom.Add(sizervbl, 1, wx.ALIGN_CENTER|wx.EXPAND)
        # imageSizerBottom.Add(lineDisplay, 0, wx.ALIGN_CENTER|wx.EXPAND)
        # imageSizerBottom.Add(sizervbr, 1, wx.ALIGN_CENTER|wx.EXPAND)



        emptyView = MicroscopeEmptyView()
        # display : left and right view
        self.displays = []
        #[(emptyView, self.viewComboLeft, self.imageSizerBLeft),
        #                 (emptyView, self.viewComboRight, self.imageSizerBRight)]

        # can be called only with display ready
        self.views = []
        self.AddView(emptyView)
        self.AddView(MicroscopeOpticalView(self, self.secom_model, self.viewmodel))
        self.AddView(MicroscopeSEView(self, self.secom_model, self.viewmodel))

        # Select the default views
        #self.ChangeView(0, self.views[1].name)
        #self.ChangeView(1, self.views[2].name)

        # sync microscope stage with the view
        self.viewmodel.center.value = self.secom_model.stage_pos.value
        self.viewmodel.center.subscribe(self.onViewCenter)

        self.SetSizer(mainSizer)
        self.SetAutoLayout(True)
        mainSizer.Fit(self)

        self.Bind(wx.EVT_SIZE, self.OnSize)

    def OnComboLeft(self, event):
        self.ChangeView(0, event.GetString())

    def OnComboRight(self, event):
        self.ChangeView(1, event.GetString())

    def OnSlider(self, event):
        """
        Merge ratio slider
        """
        self.viewmodel.merge_ratio.value = self.mergeSlider.GetValue() / 100.0

    def avOnMergeRatio(self, val):
        # round is important because int can cause unstable value
        # int(0.58*100) = 57
        self.mergeSlider.SetValue(round(val * 100))


    # We link only one way the position:
    #  * if the user moves the view => moves the stage to the same position
    #  * if the stage moves by itself, keep the view at the same place
    #    (and the acquired images will not be centred anymore)
    def onViewCenter(self, pos):
        self.secom_model.stage_pos.value = pos

    def avOnMPP(self, mpp):
        self.scaleDisplay.SetMPP(mpp)
        self.UpdateHFW()

    def OnSize(self, event):
        event.Skip() # process also by the parent
        self.UpdateHFW()

    def UpdateHFW(self):
        """ Optional. Physical width of the display"""
        hfw = self.viewmodel.mpp.value * self.GetClientSize()[0]
        label = "HFW: %sm" % units.to_string_si_prefix(hfw)
        self.hfwDisplay.SetLabel(label)

#    # Change picture one/two
#    def SetImage(self, index, im, pos = None, mpp = None):
#        self.canvas.SetImage(index, im, pos, mpp)
#
    def AddView(self, view):
        self.views.append(view)

        # update the combo boxes
        for d in self.displays:
            d[1].Append(view.name)

    def ChangeView(self, display, viewName):
        """
        Select a view and update the legend with it
        If selecting a view already displayed on the other side, it will swap them
        If less than 2 non-empty views => slider is disabled
        display: index of the display to update
        viewName (string): the name of the view
        combo: the combobox which has to be updated
        sizer: the sizer containing the controls
        """
        # find the view
        view = None
        for v in self.views:
            if v.name == viewName:
                view = v
                break
        if not view:
            raise LookupError("Unknown view " + viewName)

        (prevView, combo, sizer) = self.displays[display]
        oppDisplay = 1 - display
        (oppView, oppCombo, oppSizer) = self.displays[oppDisplay]

        needSwap = ((oppView == view) and not isinstance(view, MicroscopeEmptyView))

        # Remove old view(s)
        prevView.Hide(combo, sizer)
        if needSwap:
            oppView.Hide(oppCombo, oppSizer)
            oppView = prevView

        # Show new view
        view.Show(combo, sizer, self.viewmodel.images[display])
        self.displays[display] = (view, combo, sizer)
        if needSwap:
            oppView.Show(oppCombo, oppSizer, self.viewmodel.images[oppDisplay])
            self.displays[oppDisplay] = (oppView, oppCombo, oppSizer)

        # Remove slider if not 2 views
        if isinstance(view, MicroscopeEmptyView) or isinstance(oppView, MicroscopeEmptyView):
            self.mergeSlider.Hide()
        else:
            self.mergeSlider.Show()

        # TODO: find out if that's the nice behaviour, or should just keep it?
        if needSwap:
            self.viewmodel.merge_ratio.value = (1.0 -  self.viewmodel.merge_ratio.value)

        assert(self.displays[0] != self.displays[1] or
               isinstance(self.displays[0], MicroscopeEmptyView))




class MicroscopeView(object):
    """
    Interface for defining a type of view from the microscope (such as CCD, SE...) with all
    its values in legend.
    """
    def __init__(self, name):
        """
        name (string): user friendly name
        """
        self.name = name #
        self.legendCtrl = [] # list of wx.Control to display in the legend
        self.outimage = None # ActiveValue of instrumental image
        self.inimage = InstrumentalImage(None, None, None) # instrumental image
        self.sizer = None

    def Hide(self, combo, sizer):
        # Remove and hide all the previous controls in the sizer
        for c in self.legendCtrl:
            sizer.Detach(c)
            c.Hide()

        # For spacers: everything else in the sizer
        for c in sizer.GetChildren():
            sizer.Remove(0)

        if self.outimage:
            self.outimage.value = self.inimage

        self.sizer = None

    def Show(self, combo, sizer, outimage):
        self.outimage = outimage
        self.UpdateImage()
        self.sizer = sizer

        # Put the new controls
        first = True
        for c in self.legendCtrl:
            if first:
                first = False
            else:
                sizer.AddStretchSpacer()
            sizer.Add(c, 0, wx.ALIGN_CENTER_HORIZONTAL|wx.LEFT|wx.RIGHT|wx.EXPAND, 3)
            c.Show()

        #Update the combobox
        combo.Selection = combo.FindString(self.name)

        sizer.Layout()

    def UpdateImage(self):
        if self.outimage:
            self.outimage.value = self.inimage

class MicroscopeEmptyView(MicroscopeView):
    """
    Special view containing nothing
    """
    def __init__(self, name="None"):
        MicroscopeView.__init__(self, name)

class MicroscopeImageView(MicroscopeView):
    def __init__(self, parent, iim, viewmodel, name="Image"):
        MicroscopeView.__init__(self, name)

        self.viewmodel = viewmodel
        self.LegendMag = wx.StaticText(parent)
        self.legendCtrl.append(self.LegendMag)

        iim.subscribe(self.avImage)
        viewmodel.mpp.subscribe(self.avMPP, True)

    def avImage(self, value):
        self.inimage = value
        # This method might be called from any thread
        # GUI can be updated only from the GUI thread, so just send an event
        wx.CallAfter(self.UpdateImage)
        wx.CallAfter(self.avMPP, None)

    def avMPP(self, unused):
        # TODO: shall we use the real density of the screen?
        # We could use real density but how much important is it?
        mppScreen = 0.00025 # 0.25 mm/px
        label = "Mag: "
        if self.inimage.mpp:
            magIm = mppScreen / self.inimage.mpp # as if 1 im.px == 1 sc.px
            if magIm >= 1:
                label += "×" + str(units.round_significant(magIm, 3))
            else:
                label += "/" + str(units.round_significant(1.0/magIm, 3))
            magDig =  self.inimage.mpp / self.viewmodel.mpp.value
            if magDig >= 1:
                label += " ×" + str(units.round_significant(magDig, 3))
            else:
                label += " /" + str(units.round_significant(1.0/magDig, 3))
        self.LegendMag.SetLabel(label)

        if self.sizer:
            self.sizer.Layout()

class MicroscopeOpticalView(MicroscopeImageView):
    def __init__(self, parent, datamodel, viewmodel, name="Optical"):
        MicroscopeImageView.__init__(self, parent, datamodel.optical_det_image,
                                     viewmodel, name)

        self.datamodel = datamodel
        self.viewmodel = viewmodel

        self.LegendWl = wx.StaticText(parent)
        self.LegendET = wx.StaticText(parent)
        self.legendCtrl += [self.LegendWl, self.LegendET]

        datamodel.optical_emt_wavelength.subscribe(self.avWavelength)
        datamodel.optical_det_wavelength.subscribe(self.avWavelength, True)
        datamodel.optical_det_exposure_time.subscribe(self.avExposureTime, True)

    def avWavelength(self, value):
        # need to know both wavelengthes, so just look into the values
        win = self.datamodel.optical_emt_wavelength.value
        wout = self.datamodel.optical_det_wavelength.value

        label = "Wavelength: " + str(win) + "nm/" + str(wout) + "nm"
        self.LegendWl.SetLabel(label)

    def avExposureTime(self, value):
        label = "Exposure: %ss" % units.to_string_si_prefix(value)
        self.LegendET.SetLabel(label)

class MicroscopeSEView(MicroscopeImageView):
    def __init__(self, parent, datamodel, viewmodel, name="SE Detector"):
        MicroscopeImageView.__init__(self, parent, datamodel.sem_det_image,
                                     viewmodel, name)

        self.datamodel = datamodel
        self.viewmodel = viewmodel

        self.LegendDwell = wx.StaticText(parent)
        self.LegendSpot = wx.StaticText(parent)
        self.LegendHV = wx.StaticText(parent)
        self.legendCtrl += [ self.LegendDwell, self.LegendSpot,
                           self.LegendHV]

        datamodel.sem_emt_dwell_time.subscribe(self.avDwellTime, True)
        datamodel.sem_emt_spot.subscribe(self.avSpot, True)
        datamodel.sem_emt_hv.subscribe(self.avHV, True)

    # TODO need to use the right dimensions for the units
    def avDwellTime(self, value):
        label = "Dwell: %ss" % units.to_string_si_prefix(value)
        self.LegendDwell.SetLabel(label)

    def avSpot(self, value):
        label = "Spot: %g" % value
        self.LegendSpot.SetLabel(label)

    def avHV(self, value):
        label = "HV: %sV" % units.to_string_si_prefix(value)
        self.LegendHV.SetLabel(label)

# vim:tabstop=4:shiftwidth=4:expandtab:spelllang=en_gb:spell: