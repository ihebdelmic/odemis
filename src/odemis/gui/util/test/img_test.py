# -*- coding: utf-8 -*-
'''
Created on 19 Sep 2012

@author: piel

Copyright © 2012-2013 Éric Piel & Kimon Tsitsikas, Delmic

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

import cairo
import logging
import numpy
import os
import time
import unittest
import wx

from odemis import model, dataio
from odemis.acq import stream
from odemis.gui.util import img
from odemis.gui.util.img import wxImage2NDImage, format_rgba_darray, insert_tile_to_image, merge_screen
from odemis.dataio import tiff
from odemis.acq.stream import SinglePointSpectrumProjection, \
    RGBSpatialProjection, LineSpectrumProjection, \
    SinglePointTemporalProjection

logging.getLogger().setLevel(logging.DEBUG)


def GetRGB(im, x, y):
    """
    return the r,g,b tuple corresponding to a pixel
    """
    r = im.GetRed(x, y)
    g = im.GetGreen(x, y)
    b = im.GetBlue(x, y)

    return r, g, b


class TestWxImage2NDImage(unittest.TestCase):

    def test_simple(self):
        size = (32, 64)
        wximage = wx.Image(*size) # black RGB
        ndimage = wxImage2NDImage(wximage)
        self.assertEqual(ndimage.shape[0:2], size[-1:-3:-1])
        self.assertEqual(ndimage.shape[2], 3) # RGB
        self.assertTrue((ndimage[0, 0] == [0, 0, 0]).all())

    # TODO alpha channel


class TestRGBA(unittest.TestCase):

    def test_rgb_to_bgra(self):
        size = (32, 64, 3)
        rgbim = model.DataArray(numpy.zeros(size, dtype=numpy.uint8))
        rgbim[:, :, 0] = 1
        rgbim[:, :, 1] = 100
        rgbim[:, :, 2] = 200
        bgraim = format_rgba_darray(rgbim, 255)

        # Checks it added alpha channel
        self.assertEqual(bgraim.shape, (32, 64, 4))
        self.assertEqual(bgraim[0, 0, 3], 255)
        # Check the channels were swapped to BGR
        self.assertTrue((bgraim[1, 1] == [200, 100, 1, 255]).all())

    def test_rgb_alpha_to_bgra(self):
        size = (32, 64, 3)
        rgbim = model.DataArray(numpy.zeros(size, dtype=numpy.uint8))
        rgbim[:, :, 0] = 1
        rgbim[:, :, 1] = 100
        rgbim[:, :, 2] = 200
        bgraim = format_rgba_darray(rgbim, 0)

        # Checks it added alpha channel and set everything to scale
        self.assertEqual(bgraim.shape, (32, 64, 4))
        self.assertTrue((bgraim == 0).all())

    def test_rgba_to_bgra(self):
        size = (32, 64, 4)
        rgbaim = model.DataArray(numpy.zeros(size, dtype=numpy.uint8))
        rgbaim[:, :, 0] = 1
        rgbaim[:, :, 1] = 100
        rgbaim[:, :, 2] = 200
        rgbaim[:, :, 3] = 255
        rgbaim[2, 2, 3] = 0
        bgraim = format_rgba_darray(rgbaim)

        # Checks it added alpha channel
        self.assertEqual(bgraim.shape, (32, 64, 4))
        # Check the channels were swapped to BGR
        self.assertTrue((bgraim[1, 1] == [200, 100, 1, 255]).all())
        self.assertTrue((bgraim[2, 2] == [200, 100, 1, 0]).all())


class TestARExport(unittest.TestCase):
    FILENAME_RAW = "test-ar.csv"
    FILENAME_PR = "test-ar.png"

    def tearDown(self):
        # clean up
        try:
            os.remove(self.FILENAME_RAW)
        except Exception:
            pass

        try:
            os.remove(self.FILENAME_PR)
        except Exception:
            pass

    def test_ar_frame(self):
        ar_margin = 100
        img_size = 512, 512
        ar_size = img_size[0] + ar_margin, img_size[1] + ar_margin
        data_to_draw = numpy.zeros((ar_size[1], ar_size[0], 4), dtype=numpy.uint8)
        surface = cairo.ImageSurface.create_for_data(
            data_to_draw, cairo.FORMAT_ARGB32, ar_size[0], ar_size[1])
        ctx = cairo.Context(surface)
        app = wx.App()  # needed for the gui font name
        font_name = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT).GetFaceName()
        ticksize = 10
        num_ticks = 6
        ticks_info = img.ar_create_tick_labels(img_size, ticksize, num_ticks, ar_margin / 2)
        ticks, (center_x, center_y), inner_radius, radius = ticks_info
        # circle expected just on the center of the frame
        self.assertEqual((center_x, center_y), (ar_size[0] / 2, ar_size[1] / 2))
        self.assertLess(radius, ar_size[0] / 2)  # circle radius within limits
        img.draw_ar_frame(ctx, ar_size, ticks, font_name, center_x, center_y, inner_radius, radius)
        self.assertEqual(data_to_draw.shape[:2], ar_size)  # frame includes the margin

    def test_ar_raw(self):
        data = model.DataArray(numpy.zeros((256, 256)), metadata={model.MD_AR_POLE: (100, 100),
                                                                  model.MD_PIXEL_SIZE: (1e-03, 1e-03)})
        raw_polar = img.calculate_raw_ar(data, data)
        # shape = raw data + theta/phi axes values
        self.assertGreater(raw_polar.shape[0], 50)
        self.assertGreater(raw_polar.shape[1], 50)

    def test_ar_raw_rect(self):
        data = model.DataArray(numpy.zeros((256, 512)), metadata={model.MD_AR_POLE: (100, 250),
                                                                  model.MD_PIXEL_SIZE: (1e-03, 1e-03)})
        raw_polar = img.calculate_raw_ar(data, data)
        # shape = raw data + theta/phi axes values
        self.assertGreater(raw_polar.shape[0], 50)
        self.assertGreater(raw_polar.shape[1], 50)

    def test_ar_export(self):

        # Create AR data
        md = {
            model.MD_SW_VERSION: "1.0-test",
            model.MD_HW_NAME: "fake ccd",
            model.MD_DESCRIPTION: "AR",
            model.MD_ACQ_TYPE: model.MD_AT_AR,
            model.MD_ACQ_DATE: time.time(),
            model.MD_BPP: 12,
            model.MD_BINNING: (1, 1),  # px, px
            model.MD_SENSOR_PIXEL_SIZE: (13e-6, 13e-6),  # m/px
            model.MD_PIXEL_SIZE: (5.2e-5, 5.2e-5),  # m/px
            model.MD_POS: (1.2e-3, -30e-3),  # m
            model.MD_EXP_TIME: 1.2,  # s
            model.MD_AR_POLE: (283, 259),
        }

        md0 = dict(md)
        data0 = model.DataArray(1500 + numpy.zeros((512, 512), dtype=numpy.uint16), md0)
        md1 = dict(md)
        md1[model.MD_POS] = (1.5e-3, -30e-3)
        md1[model.MD_BASELINE] = 300  # AR background should take this into account
        data1 = model.DataArray(3345 + numpy.zeros((512, 512), dtype=numpy.uint16), md1)

        # Create AR stream
        ars = stream.StaticARStream("test", [data0, data1])
        ars.point.value = md1[model.MD_POS]

        # Wait for the projection to be computed
        tend = time.time() + 30
        while ars.image.value is None:
            self.assertLess(time.time(), tend, "Timeout during AR computation")
            time.sleep(0.1)

        # Convert to exportable RGB image
        exdata = img.ar_to_export_data([ars], raw=False)
        # shape = RGBA
        self.assertGreater(exdata.shape[0], 200)
        self.assertGreater(exdata.shape[1], 200)
        self.assertEqual(exdata.shape[2], 4)

        # The top-left corner should be white
        numpy.testing.assert_equal(exdata[0, 0], [255, 255, 255, 255])
        # There should be some non-white data
        self.assertTrue(numpy.any(exdata != 255))

        # Save into a PNG file
        exporter = dataio.get_converter("PNG")
        exporter.export(self.FILENAME_PR, exdata)
        st = os.stat(self.FILENAME_PR)  # this test also that the file is created
        self.assertGreater(st.st_size, 1000)

        # Convert to exportable RGB image
        exdata = img.ar_to_export_data([ars], raw=True)
        # shape = raw data + theta/phi axes values
        self.assertGreater(exdata.shape[0], 50)
        self.assertGreater(exdata.shape[1], 50)

        # Save into a CSV file
        exporter = dataio.get_converter("CSV")
        exporter.export(self.FILENAME_RAW, exdata)
        st = os.stat(self.FILENAME_RAW)  # this test also that the file is created
        self.assertGreater(st.st_size, 100)

    # TODO: check that exporting large AR image doesn't get crazy memory usage

    def test_big_ar_export(self):

        # Create AR data
        md = {
            model.MD_SW_VERSION: "1.0-test",
            model.MD_HW_NAME: "fake ccd",
            model.MD_DESCRIPTION: "AR",
            model.MD_ACQ_TYPE: model.MD_AT_AR,
            model.MD_ACQ_DATE: time.time(),
            model.MD_BPP: 12,
            model.MD_BINNING: (1, 1),  # px, px
            model.MD_SENSOR_PIXEL_SIZE: (13e-6, 13e-6),  # m/px
            model.MD_PIXEL_SIZE: (4e-5, 4e-5),  # m/px
            model.MD_POS: (1.2e-3, -30e-3),  # m
            model.MD_EXP_TIME: 1.2,  # s
            model.MD_AR_POLE: (500, 500),
        }

        md0 = dict(md)
        data0 = model.DataArray(1500 + numpy.zeros((1080, 1024), dtype=numpy.uint16), md0)
        md1 = dict(md)
        md1[model.MD_POS] = (1.5e-3, -30e-3)
        md1[model.MD_BASELINE] = 300  # AR background should take this into account
        data1 = model.DataArray(500 + numpy.zeros((1080, 1024), dtype=numpy.uint16), md1)

        # Create AR stream
        ars = stream.StaticARStream("test", [data0])
        ars.point.value = md0[model.MD_POS]

        # Wait for the projection to be computed
        tend = time.time() + 90
        while ars.image.value is None:
            self.assertLess(time.time(), tend, "Timeout during AR computation")
            time.sleep(0.1)

        # Convert to exportable RGB image
        exdata = img.ar_to_export_data([ars], raw=False)
        # shape = RGBA
        self.assertGreater(exdata.shape[0], 200)
        self.assertGreater(exdata.shape[1], 200)
        self.assertEqual(exdata.shape[2], 4)

        # The top-left corner should be white
        numpy.testing.assert_equal(exdata[0, 0], [255, 255, 255, 255])
        # There should be some non-white data
        self.assertTrue(numpy.any(exdata != 255))

        # Save into a PNG file
        exporter = dataio.get_converter("PNG")
        exporter.export(self.FILENAME_PR, exdata)
        st = os.stat(self.FILENAME_PR)  # this test also that the file is created
        self.assertGreater(st.st_size, 1000)

        # Convert to equirectangular (RAW) image
        exdata = img.ar_to_export_data([ars], raw=True)
        # shape = raw data + theta/phi axes values
        self.assertGreater(exdata.shape[0], 50)
        self.assertGreater(exdata.shape[1], 50)

        # Save into a CSV file
        exporter = dataio.get_converter("CSV")
        exporter.export(self.FILENAME_RAW, exdata)
        st = os.stat(self.FILENAME_RAW)  # this test also that the file is created
        self.assertGreater(st.st_size, 100)

        # Create AR stream with background image
        ars.background.value = data1

        # Convert to equirectangular (RAW) image
        exdata = img.ar_to_export_data([ars], raw=True)
        # shape = raw data + theta/phi axes values
        self.assertGreater(exdata.shape[0], 50)
        self.assertGreater(exdata.shape[1], 50)


class TestSpectrumExport(unittest.TestCase):

    def setUp(self):
        data = numpy.ones((251, 3, 1, 200, 300), dtype="uint16")
        data[:, 0, 0, :, 3] = range(200)
        data[:, 0, 0, :, 3] *= 3
        data[:, 0, 0, 1, 3] = range(251)
        data[2, 0, 0, :, :] = range(300)
        data[200, 0, 0, 2, :] = range(300)
        wld = list(433e-9 + numpy.array(range(data.shape[0])) * 0.1e-9)
        tld = list(numpy.array(range(data.shape[1])) * 0.1e-9)
        md = {model.MD_SW_VERSION: "1.0-test",
             model.MD_HW_NAME: "fake ccd",
             model.MD_DESCRIPTION: "Spectrum",
             model.MD_ACQ_DATE: time.time(),
             model.MD_BPP: 12,
             model.MD_PIXEL_SIZE: (2e-6, 2e-6),  # m/px
             model.MD_POS: (-0.001203511795256, -0.000295338300158),  # m
             model.MD_EXP_TIME: 0.2,  # s
             model.MD_LENS_MAG: 60,  # ratio
             model.MD_WL_LIST: wld,
             model.MD_TIME_LIST: tld,
            }
        self.spec_data = model.DataArray(data, md)
        self.spec_stream = stream.StaticSpectrumStream("test spec", self.spec_data)
        self.spec_stream.selected_pixel.value = (3, 1)
        # self.app = wx.App()  # needed for the gui font name

    def test_spectrum_ready(self):
        self.spec_stream.selectionWidth.value = 1
        proj = SinglePointSpectrumProjection(self.spec_stream)
        exported_data = img.spectrum_to_export_data(proj, False)
        self.assertEqual(exported_data.metadata[model.MD_DIMS], 'YXC')  # ready for RGB export
        self.assertEqual(exported_data.shape[:2],
                         (img.SPEC_PLOT_SIZE + img.SPEC_SCALE_HEIGHT + img.SMALL_SCALE_WIDTH,
                          img.SPEC_PLOT_SIZE + img.SPEC_SCALE_WIDTH + img.SMALL_SCALE_WIDTH))  # exported image includes scale bars

        self.spec_stream.selectionWidth.value = 4
        exported_data = img.spectrum_to_export_data(proj, False)
        self.assertEqual(exported_data.metadata[model.MD_DIMS], 'YXC')  # ready for RGB export
        self.assertEqual(exported_data.shape[:2],
                         (img.SPEC_PLOT_SIZE + img.SPEC_SCALE_HEIGHT + img.SMALL_SCALE_WIDTH,
                          img.SPEC_PLOT_SIZE + img.SPEC_SCALE_WIDTH + img.SMALL_SCALE_WIDTH))  # exported image includes scale bars

    def test_spectrum_temporal(self):
        self.spec_stream.selectionWidth.value = 1
        proj = SinglePointTemporalProjection(self.spec_stream)
        exported_data = img.chronogram_to_export_data(proj, False)
        self.assertEqual(exported_data.metadata[model.MD_DIMS], 'YXC')  # ready for RGB export
        self.assertEqual(exported_data.shape[:2],
                         (img.SPEC_PLOT_SIZE + img.SPEC_SCALE_HEIGHT + img.SMALL_SCALE_WIDTH,
                          img.SPEC_PLOT_SIZE + img.SPEC_SCALE_WIDTH + img.SMALL_SCALE_WIDTH))  # exported image includes scale bars

        self.spec_stream.selectionWidth.value = 4
        exported_data = img.chronogram_to_export_data(proj, False)
        self.assertEqual(exported_data.metadata[model.MD_DIMS], 'YXC')  # ready for RGB export
        self.assertEqual(exported_data.shape[:2],
                         (img.SPEC_PLOT_SIZE + img.SPEC_SCALE_HEIGHT + img.SMALL_SCALE_WIDTH,
                          img.SPEC_PLOT_SIZE + img.SPEC_SCALE_WIDTH + img.SMALL_SCALE_WIDTH))  # exported image includes scale bars

    def test_spectrum_raw(self):

        filename = "test-spec-spot.csv"

        self.spec_stream.selectionWidth.value = 1
        proj = SinglePointSpectrumProjection(self.spec_stream)
        exported_data = img.spectrum_to_export_data(proj, True)
        self.assertEqual(exported_data.shape[0], self.spec_data.shape[0])  # exported image includes only raw data

        # Save into a CSV file
        exporter = dataio.get_converter("CSV")
        exporter.export(filename, exported_data)
        st = os.stat(filename)  # this test also that the file is created
        self.assertGreater(st.st_size, 10)

        self.spec_stream.selectionWidth.value = 3
        exported_data = img.spectrum_to_export_data(proj, True)
        self.assertEqual(exported_data.shape[0], self.spec_data.shape[0])  # exported image includes only raw data

        # Save into a CSV file
        exporter = dataio.get_converter("CSV")
        exporter.export(filename, exported_data)
        st = os.stat(filename)  # this test also that the file is created
        self.assertGreater(st.st_size, 10)

        # clean up
        try:
            os.remove(filename)
        except Exception:
            pass
        
    def test_chronogram_raw(self):

        filename = "test-spec-spot.csv"

        self.spec_stream.selectionWidth.value = 1
        proj = SinglePointTemporalProjection(self.spec_stream)
        exported_data = img.chronogram_to_export_data(proj, True)
        self.assertEqual(exported_data.shape[0], self.spec_data.shape[1])  # exported image includes only raw data

        # Save into a CSV file
        exporter = dataio.get_converter("CSV")
        exporter.export(filename, exported_data)
        st = os.stat(filename)  # this test also that the file is created
        self.assertGreater(st.st_size, 10)

        self.spec_stream.selectionWidth.value = 3
        exported_data = img.chronogram_to_export_data(proj, True)
        self.assertEqual(exported_data.shape[0], self.spec_data.shape[1])  # exported image includes only raw data

        # Save into a CSV file
        exporter = dataio.get_converter("CSV")
        exporter.export(filename, exported_data)
        st = os.stat(filename)  # this test also that the file is created
        self.assertGreater(st.st_size, 10)

        # clean up
        try:
            os.remove(filename)
        except Exception:
            pass


class TestSpectrumLineExport(unittest.TestCase):

    def setUp(self):
        data = numpy.ones((251, 1, 1, 200, 300), dtype="uint16")
        data[:, 0, 0, :, 3] = range(200)
        data[:, 0, 0, :, 3] *= 3
        data[:, 0, 0, 1, 3] = range(251)
        data[2, :, :, :, :] = range(300)
        data[200, 0, 0, 2] = range(300)
        wld = 433e-9 + numpy.array(range(data.shape[0])) * 0.1e-9
        md = {model.MD_SW_VERSION: "1.0-test",
             model.MD_HW_NAME: "fake ccd",
             model.MD_DESCRIPTION: "Spectrum",
             model.MD_ACQ_DATE: time.time(),
             model.MD_BPP: 12,
             model.MD_PIXEL_SIZE: (2e-6, 2e-6),  # m/px
             model.MD_POS: (-0.001203511795256, -0.000295338300158),  # m
             model.MD_EXP_TIME: 0.2,  # s
             model.MD_LENS_MAG: 60,  # ratio
             model.MD_WL_LIST: wld,
            }
        self.spec_data = model.DataArray(data, md)
        self.spec_stream = stream.StaticSpectrumStream("test spec", self.spec_data)
        self.spec_stream.selected_line.value = ((3, 1), (235, 65))
        self.app = wx.App()  # needed for the gui font name

    def test_line_ready(self):
        self.spec_stream.selectionWidth.value = 1
        proj = LineSpectrumProjection(self.spec_stream)
        exported_data = img.line_to_export_data(proj, False)
        self.assertEqual(exported_data.metadata[model.MD_DIMS], 'YXC')  # ready for RGB export
        self.assertEqual(exported_data.shape[:2],
                         (img.SPEC_PLOT_SIZE + img.SPEC_SCALE_HEIGHT + img.SMALL_SCALE_WIDTH,
                          img.SPEC_PLOT_SIZE + img.SPEC_SCALE_WIDTH + img.SMALL_SCALE_WIDTH))  # exported image includes scale bars

        self.spec_stream.selectionWidth.value = 3
        exported_data = img.line_to_export_data(proj, False)
        self.assertEqual(exported_data.metadata[model.MD_DIMS], 'YXC')  # ready for RGB export
        self.assertEqual(exported_data.shape[:2],
                         (img.SPEC_PLOT_SIZE + img.SPEC_SCALE_HEIGHT + img.SMALL_SCALE_WIDTH,
                          img.SPEC_PLOT_SIZE + img.SPEC_SCALE_WIDTH + img.SMALL_SCALE_WIDTH))  # exported image includes scale bars

    def test_line_raw(self):
        filename = "test-spec-line.csv"

        self.spec_stream.selectionWidth.value = 1
        proj = LineSpectrumProjection(self.spec_stream)
        exported_data = img.line_to_export_data(proj, True)
        self.assertEqual(exported_data.shape[1], self.spec_data.shape[0])
        self.assertGreater(exported_data.shape[0], 64)  # at least 65-1 px
        self.assertEqual(exported_data.metadata[model.MD_DIMS],"XC")

        # Save into a CSV file
        exporter = dataio.get_converter("CSV")
        exporter.export(filename, exported_data)
        st = os.stat(filename)  # this test also that the file is created
        self.assertGreater(st.st_size, 100)

        self.spec_stream.selectionWidth.value = 4
        exported_data = img.line_to_export_data(proj, True)
        self.assertEqual(exported_data.shape[1], self.spec_data.shape[0])
        self.assertGreater(exported_data.shape[0], 64)  # at least 65-1 px
        self.assertEqual(exported_data.metadata[model.MD_DIMS],"XC")

        # Save into a CSV file
        exporter = dataio.get_converter("CSV")
        exporter.export(filename, exported_data)
        st = os.stat(filename)  # this test also that the file is created
        self.assertGreater(st.st_size, 100)

        # clean up
        try:
            os.remove(filename)
        except Exception:
            pass


class TestSpatialExport(unittest.TestCase):

    def setUp(self):
        self.app = wx.App()
        data = numpy.zeros((2160, 2560), dtype=numpy.uint16)
        dataRGB = numpy.zeros((2160, 2560, 4))
        metadata = {'Hardware name': 'Andor ZYLA-5.5-USB3 (s/n: VSC-01959)',
                    'Exposure time': 0.3, 'Pixel size': (1.59604600574173e-07, 1.59604600574173e-07),
                    'Acquisition date': 1441361559.258568, 'Hardware version': "firmware: '14.9.16.0' (driver 3.10.30003.5)",
                    'Centre position': (-0.001203511795256, -0.000295338300158), 'Lens magnification': 40.0,
                    'Input wavelength range': (6.15e-07, 6.350000000000001e-07), 'Shear':-4.358492733391727e-16,
                    'Description': 'Filtered colour 1', 'Bits per pixel': 16, 'Binning': (1, 1), 'Pixel readout time': 1e-08,
                    'Gain': 1.1, 'Rotation': 6.279302551026012, 'Light power': 0.0, 'Display tint': (255, 0, 0),
                    'Output wavelength range': (6.990000000000001e-07, 7.01e-07)}
        image = model.DataArray(data, metadata)
        fluo_stream = stream.StaticFluoStream(metadata['Description'], image)
        #fluo_stream.image.value = model.DataArray(dataRGB, metadata)

        data = numpy.zeros((1024, 1024), dtype=numpy.uint16)
        dataRGB = numpy.zeros((1024, 1024, 4))
        metadata = {'Hardware name': 'pcie-6251', 'Description': 'Secondary electrons',
                    'Exposure time': 3e-06, 'Pixel size': (5.9910982493639e-08, 6.0604642506361e-08),
                    'Acquisition date': 1441361562.0, 'Hardware version': 'Unknown (driver 2.1-160-g17a59fb (driver ni_pcimio v0.7.76))',
                    'Centre position': (-0.001203511795256, -0.000295338300158), 'Lens magnification': 5000.0, 'Rotation': 0.0,
                    'Shear': 0.003274715695854}
        image = model.DataArray(data, metadata)
        sem_stream = stream.StaticSEMStream(metadata['Description'], image)
        #sem_stream.image.value = model.DataArray(dataRGB, metadata)
        # create DataProjections for the streams
        fluo_stream_pj = stream.RGBSpatialProjection(fluo_stream)
        sem_stream_pj = stream.RGBSpatialProjection(sem_stream)
        self.streams = [fluo_stream_pj, sem_stream_pj]
        self.min_res = (623, 432)

        # Spectrum stream
        data = numpy.ones((251, 1, 1, 200, 300), dtype="uint16")
        data[:, 0, 0, :, 3] = range(200)
        data[:, 0, 0, :, 3] *= 3
        data[:, 0, 0, 1, 3] = range(251)
        data[2, :, :, :, :] = range(300)
        data[200, 0, 0, 2] = range(300)
        wld = 433e-9 + numpy.array(range(data.shape[0])) * 0.1e-9
        tld = model.DataArray(range(data.shape[1])) * 0.1e-9
        md = {model.MD_SW_VERSION: "1.0-test",
             model.MD_HW_NAME: "fake ccd",
             model.MD_DESCRIPTION: "Spectrum",
             model.MD_ACQ_DATE: time.time(),
             model.MD_BPP: 12,
             model.MD_PIXEL_SIZE: (2e-6, 2e-6), # m/px
             model.MD_POS: (-0.001203511795256, -0.000295338300158), # m
             model.MD_EXP_TIME: 0.2, # s
             model.MD_LENS_MAG: 60, # ratio
             model.MD_WL_LIST: wld,
             model.MD_TIME_LIST: tld,
            }
        spec_data = model.DataArray(data, md)
        self.spec_stream = stream.StaticSpectrumStream("test spec", spec_data)

        # Wait for all the streams to get an RGB image
        time.sleep(0.5)

    def test_spec_pr(self):
        view_hfw = (0.00025158414075691866, 0.00017445320835792754)
        view_pos = [-0.001211588332679978, -0.00028726176273402186]
        draw_merge_ratio = 0.3
        proj = RGBSpatialProjection(self.spec_stream)
        streams = [self.streams[1], proj]
        orig_md = [s.raw[0].metadata.copy() for s in streams]
        exp_data = img.images_to_export_data(streams, view_hfw, view_pos, draw_merge_ratio, False)
        self.assertEqual(exp_data[0].shape, (3379, 4199, 4))  # RGB
        for s, md in zip(streams, orig_md):
            self.assertEqual(md, s.raw[0].metadata)

    def test_spec_pp(self):
        view_hfw = (0.00025158414075691866, 0.00017445320835792754)
        view_pos = [-0.001211588332679978, -0.00028726176273402186]
        draw_merge_ratio = 0.3
        proj = RGBSpatialProjection(self.spec_stream)
        streams = [self.streams[1], proj]
        orig_md = [s.raw[0].metadata.copy() for s in streams]
        exp_data = img.images_to_export_data(streams, view_hfw, view_pos, draw_merge_ratio, True)
        self.assertEqual(exp_data[0].shape, (3379, 4199))  # greyscale
        for s, md in zip(streams, orig_md):
            self.assertEqual(md, s.raw[0].metadata)

    def test_no_crop_need(self):
        """
        Data roi covers the whole window view
        """
        view_hfw = (0.00025158414075691866, 0.00017445320835792754)
        view_pos = [-0.001211588332679978, -0.00028726176273402186]
        draw_merge_ratio = 0.3
        exp_data = img.images_to_export_data([self.streams[0]], view_hfw, view_pos, draw_merge_ratio, False)
        self.assertEqual(exp_data[0].shape, (1226, 1576, 4))  # RGB
        self.assertEqual(len(exp_data), 1)

        # test also raw export
        exp_data = img.images_to_export_data([self.streams[0]], view_hfw, view_pos, draw_merge_ratio, True)
        self.assertEqual(exp_data[0].shape, (1226, 1576))  # greyscale

    def test_crop_need(self):
        """
        Data roi covers part of the window view thus we need to crop the
        intersection with the data
        """
        view_hfw = (0.0005031682815138373, 0.0003489064167158551)
        view_pos = [-0.001211588332679978, -0.00028726176273402186]
        draw_merge_ratio = 0.3
        exp_data = img.images_to_export_data([self.streams[0]], view_hfw, view_pos, draw_merge_ratio, False)
        self.assertEqual(exp_data[0].shape, (2340, 2560, 4))  # RGB

    def test_crop_and_interpolation_need(self):
        """
        Data roi covers part of the window view and data resolution is below
        the minimum limit thus we need to interpolate the data in order to
        keep the shape ratio unchanged
        """
        view_hfw = (0.0010063365630276746, 0.0006978128334317102)
        view_pos = [-0.0015823014004405739, -0.0008081984265806109]
        draw_merge_ratio = 0.3
        exp_data = img.images_to_export_data([self.streams[0]], view_hfw, view_pos, draw_merge_ratio, False)
        self.assertEqual(exp_data[0].shape, (182, 1673, 4))  # RGB

    def test_multiple_streams(self):
        orig_md = [s.raw[0].metadata.copy() for s in self.streams]

        # Print ready format
        view_hfw = (8.191282393266523e-05, 6.205915392651362e-05)
        view_pos = [-0.001203511795256, -0.000295338300158]
        draw_merge_ratio = 0.3
        exp_data = img.images_to_export_data(self.streams, view_hfw, view_pos, draw_merge_ratio, False)
        self.assertEqual(len(exp_data), 1)
        self.assertEqual(len(exp_data[0].shape), 3)  # RGB

        # Post-process format
        exp_data = img.images_to_export_data(self.streams, view_hfw, view_pos, draw_merge_ratio, True)
        self.assertEqual(len(exp_data), 2)
        self.assertEqual(len(exp_data[0].shape), 2)  # grayscale
        self.assertEqual(len(exp_data[1].shape), 2)  # grayscale
        self.assertEqual(exp_data[0].shape, exp_data[1].shape)  # all exported images must have the same shape

        # Metadata shouldn't be modified
        for s, md in zip(self.streams, orig_md):
            self.assertEqual(md, s.raw[0].metadata)

    def test_no_intersection(self):
        """
        Data has no intersection with the window view
        """
        view_hfw = (0.0001039324002586505, 6.205915392651362e-05)
        view_pos = [-0.00147293527265202, -0.0004728408264424368]
        draw_merge_ratio = 0.3
        with self.assertRaises(LookupError):
            img.images_to_export_data(self.streams, view_hfw, view_pos, draw_merge_ratio, False)

        with self.assertRaises(LookupError):
            img.images_to_export_data(self.streams, view_hfw, view_pos, draw_merge_ratio, True)

    def test_thin_column(self):
        """
        Test that minimum width limit is fulfilled in case only a very thin
        column of data is in the view
        """
        view_hfw = (8.191282393266523e-05, 6.205915392651362e-05)
        view_pos = [-0.0014443006338779269, -0.0002968821446105185]
        draw_merge_ratio = 0.3
        exp_data = img.images_to_export_data(self.streams, view_hfw, view_pos, draw_merge_ratio, False)
        self.assertEqual(len(exp_data), 1)
        self.assertEqual(len(exp_data[0].shape), 3)  # RGB
        self.assertEqual(exp_data[0].shape[1], img.CROP_RES_LIMIT)

        exp_data = img.images_to_export_data(self.streams, view_hfw, view_pos, draw_merge_ratio, True)
        self.assertEqual(len(exp_data), len(self.streams))
        self.assertEqual(len(exp_data[0].shape), 2)  # greyscale
        self.assertEqual(exp_data[0].shape[1], img.CROP_RES_LIMIT)


class TestSpatialExportPyramidal(unittest.TestCase):

    def setUp(self):
        self.app = wx.App()
        data = numpy.zeros((2160, 2560), dtype=numpy.uint16)
        dataRGB = numpy.zeros((2160, 2560, 4))
        metadata = {'Hardware name': 'Andor ZYLA-5.5-USB3 (s/n: VSC-01959)',
                    'Exposure time': 0.3, 'Pixel size': (1.59604600574173e-07, 1.59604600574173e-07),
                    'Acquisition date': 1441361559.258568, 'Hardware version': "firmware: '14.9.16.0' (driver 3.10.30003.5)",
                    'Centre position': (-0.001203511795256, -0.000295338300158), 'Lens magnification': 40.0,
                    'Input wavelength range': (6.15e-07, 6.350000000000001e-07), 'Shear':-4.358492733391727e-16,
                    'Description': 'Filtered colour 1', 'Bits per pixel': 16, 'Binning': (1, 1), 'Pixel readout time': 1e-08,
                    'Gain': 1.1, 'Rotation': 6.279302551026012, 'Light power': 0.0, 'Display tint': (255, 0, 0),
                    'Output wavelength range': (6.990000000000001e-07, 7.01e-07)}
        image = model.DataArray(data, metadata)
        fluo_stream = stream.StaticFluoStream(metadata['Description'], image)
        fluo_stream_pj = stream.RGBSpatialProjection(fluo_stream)

        data = numpy.zeros((1024, 1024), dtype=numpy.uint16)
        dataRGB = numpy.zeros((1024, 1024, 4))
        metadata = {'Hardware name': 'pcie-6251', 'Description': 'Secondary electrons',
                    'Exposure time': 3e-06, 'Pixel size': (1e-6, 1e-6),
                    'Acquisition date': 1441361562.0, 'Hardware version': 'Unknown (driver 2.1-160-g17a59fb (driver ni_pcimio v0.7.76))',
                    'Centre position': (-0.001203511795256, -0.000295338300158), 'Lens magnification': 5000.0, 'Rotation': 0.0}
        image = model.DataArray(data, metadata)

        # export
        FILENAME = u"test" + tiff.EXTENSIONS[0]
        tiff.export(FILENAME, image, pyramid=True)
        # read back
        acd = tiff.open_data(FILENAME)
        sem_stream = stream.StaticSEMStream(metadata['Description'], acd.content[0])
        sem_stream_pj = stream.RGBSpatialProjection(sem_stream)
        sem_stream_pj.mpp.value = 1e-6

        self.streams = [fluo_stream_pj, sem_stream_pj]
        self.min_res = (623, 432)

        # Wait for all the streams to get an RGB image
        time.sleep(0.5)

    def test_first(self):
        orig_md = [s.raw[0][0].metadata.copy() for s in self.streams]

        # Print ready format
        view_hfw = (8.191282393266523e-05, 6.205915392651362e-05)
        view_pos = [-0.001203511795256, -0.000295338300158]
        draw_merge_ratio = 0.3
        self.streams[1]._updateImage()
        exp_data_rgb = img.images_to_export_data(self.streams, view_hfw, view_pos, draw_merge_ratio, False)
        self.assertEqual(len(exp_data_rgb), 1)
        self.assertEqual(len(exp_data_rgb[0].shape), 3)  # RGB

        # Post-process format
        exp_data_gray = img.images_to_export_data(self.streams, view_hfw, view_pos, draw_merge_ratio, True)
        self.assertEqual(len(exp_data_gray), 2)
        self.assertEqual(len(exp_data_gray[0].shape), 2)  # grayscale
        self.assertEqual(exp_data_rgb[0].shape[:2], exp_data_gray[0].shape)  # all exported images must have the same shape

        for s, md in zip(self.streams, orig_md):
            self.assertEqual(md, s.raw[0][0].metadata)


class TestOverviewFunctions(unittest.TestCase):
    """ Tests the util functions used in building up the overview image """

    def test_image_on_ovv(self):
        """ Tests insert_tile_to_image function """

        # Insert tile into image
        ovv_im = model.DataArray(numpy.zeros((11, 11, 3), dtype=numpy.uint8))
        ovv_im.metadata[model.MD_PIXEL_SIZE] = (1, 1)
        tile = model.DataArray(255 * numpy.ones((3, 3, 3), dtype=numpy.uint8))
        tile.metadata[model.MD_POS] = (-3, 3)
        tile.metadata[model.MD_PIXEL_SIZE] = (1, 1)
        ovv_im_new = insert_tile_to_image(tile, ovv_im)
        # rectangle with edges (-4,4), (-2,4), (-4,2), (-4,2) should be white now
        # (6,6) is the center, so this corresponds to a rectangle with top left at (2,2) (=(1,1) when counting from 0)
        numpy.testing.assert_array_equal(ovv_im_new[1:4, 1:4, :], tile)
        numpy.testing.assert_array_equal(ovv_im_new[5:, 5:, :], numpy.zeros((6, 6, 3)))

        # Test tile that goes beyond the borders of the image top left
        ovv_im = model.DataArray(numpy.zeros((5, 5, 3), dtype=numpy.uint8))
        ovv_im.metadata[model.MD_PIXEL_SIZE] = (1, 1)
        tile = model.DataArray(255 * numpy.ones((2, 2, 3), dtype=numpy.uint8))
        tile.metadata[model.MD_POS] = (-3, 3)  # top-left corner, one pixel diagonal to start of ovv
        tile.metadata[model.MD_PIXEL_SIZE] = (1, 1)
        ovv_im_new = insert_tile_to_image(tile, ovv_im)
        self.assertEqual(ovv_im_new[0][0][0], 255)  # first pixel white (all three dims), rest black
        numpy.testing.assert_array_equal(ovv_im.flatten()[3:],
                                         numpy.zeros(len(ovv_im.flatten()[3:])))

        # Test tile that goes beyond the borders of the image bottom right
        ovv_im = model.DataArray(numpy.zeros((5, 5, 3), dtype=numpy.uint8))
        ovv_im.metadata[model.MD_PIXEL_SIZE] = (1, 1)
        tile = model.DataArray(255 * numpy.ones((2, 2, 3), dtype=numpy.uint8))
        tile.metadata[model.MD_POS] = (3, -3)  # bottom-right corner, one pixel diagonal to end of ovv
        tile.metadata[model.MD_PIXEL_SIZE] = (1, 1)
        ovv_im_new = insert_tile_to_image(tile, ovv_im)
        self.assertEqual(ovv_im_new[4][4][0], 255)
        numpy.testing.assert_array_equal(ovv_im.flatten()[:-3],
                                         numpy.zeros(len(ovv_im.flatten()[3:])))

        # Test tile that lies completely outside the overview image
        ovv_im = model.DataArray(numpy.zeros((5, 5, 3), dtype=numpy.uint8))
        ovv_im.metadata[model.MD_PIXEL_SIZE] = (1, 1)
        tile = model.DataArray(255 * numpy.ones((2, 2, 3), dtype=numpy.uint8))
        tile.metadata[model.MD_POS] = (10, -10)
        tile.metadata[model.MD_PIXEL_SIZE] = (1, 1)
        ovv_im_new = insert_tile_to_image(tile, ovv_im)
        numpy.testing.assert_array_equal(ovv_im_new, numpy.zeros((5, 5, 3)))

    def test_merge(self):
        """ Tests merge_screen function """
        # Test if overview image changes after inserted optical and sem image
        md = {
            model.MD_DESCRIPTION: "green dye",
            model.MD_BPP: 12,
            model.MD_BINNING: (1, 1),  # px, px
            model.MD_PIXEL_SIZE: (1e-6, 1e-6),  # m/px
            model.MD_POS: (13.7e-3, -30e-3),  # m
            model.MD_EXP_TIME: 1,  # s
            model.MD_IN_WL: (600e-9, 620e-9),  # m
            model.MD_OUT_WL: (620e-9, 650e-9),  # m
            model.MD_USER_TINT: (0, 0, 255),  # RGB (blue)
            model.MD_ROTATION: 0.1,  # rad
            model.MD_SHEAR: 0,
            model.MD_DIMS: "YXC"
        }

        opt = model.DataArray(200 * numpy.ones((10, 10, 3), dtype=numpy.uint8), md)
        sem = model.DataArray(10 * numpy.ones((10, 10, 3), dtype=numpy.uint8), md)
        merged = merge_screen(opt, sem)
        # Test if at least one element of ovv image is different from original images
        # self.assertEqual(merged.shape, (10, 10, 3))
        self.assertTrue(numpy.all(merged >= opt))
        self.assertTrue(numpy.any(merged != opt))
        self.assertTrue(numpy.all(merged >= sem))
        self.assertTrue(numpy.any(merged != sem))

        # Two very bright images should give a complete white
        opt = model.DataArray(250 * numpy.ones((10, 10, 3), dtype=numpy.uint8), md)
        sem = model.DataArray(250 * numpy.ones((10, 10, 3), dtype=numpy.uint8), md)
        merged = merge_screen(opt, sem)
        # Test if at least one element of ovv image is different from original images
        # self.assertEqual(merged.shape, (10, 10, 3))
        self.assertTrue(numpy.all(merged == 255))

if __name__ == "__main__":
    unittest.main()

