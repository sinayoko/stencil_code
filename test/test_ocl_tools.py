from stencil_code.boundary_kernel import BoundaryCopyKernel
from stencil_code.neighborhood import Neighborhood

__author__ = 'chick'

import unittest
import numpy
import itertools
from operator import mul

from stencil_code.halo_enumerator import HaloEnumerator
from stencil_code.stencil_kernel import Stencil
from stencil_code.backend.ocl_tools import product, OclTools


class MockDevice(object):
    def __init__(self, max_work_group_size=512, max_local_group_sizes=[512, 512, 512],
                 max_compute_units=40):
        self.max_work_group_size = max_work_group_size
        self.max_local_group_sizes = max_local_group_sizes
        self.max_compute_units = max_compute_units


class TestOclTools(unittest.TestCase):
    def _are_lists_equal(self, list1, list2):
        self.assertEqual(sorted(list1), sorted(list2))

    def _are_lists_unequal(self, list1, list2):
        self.assertNotEqual(sorted(list1), sorted(list2))

    def test_prod(self):
        self.assertTrue(product([1]) == 1)
        self.assertTrue(product([2, 3, 4]) == 24)

    def test_compute_local_group_size_1d(self):
        tools = OclTools(MockDevice())

        # chooses the minimum of shape / 2 and max local group size
        self.assertTrue(
            tools.compute_local_size_1d([100]) == 50,
            "when smaller than work group divide by 2"
        )
        print("ls1d {}".format(tools.compute_local_size_1d([1000])))

        self.assertTrue(
            tools.compute_local_size_1d([1000]) == 500,
            "when smaller than work group divide by 2"
        )

        self.assertTrue(
            tools.compute_local_size_1d([10000]) == 512,
            "when smaller than work group divide by 2"
        )

    def test_compute_local_group_size_2d(self):
        # return the same kernel for MPU style device, macbookpro 2014

        # tools = OclTools(MockDevice(1024, [1024, 1, 1], 40))
        #
        # self.assertTrue(tools.compute_local_size_2d([1, 101]) == (1, 1))
        # self.assertTrue(tools.compute_local_size_2d([101, 1]) == (101, 1))

        # this device looks like a 2014 Iris Pro
        # the following numbers have not yet been tested for optimality
        # they are mostly a product of a desire for consistency and
        # minimization of unused cycles
        # TODO: fix both generator and number to reflect optimality

        tools = OclTools(MockDevice(512, [512, 512, 512], 40))

        self.assertTrue(tools.compute_local_size_2d([1, 101]) == (1, 101))
        self.assertTrue(tools.compute_local_size_2d([101, 1]) == (101, 1))

        self.assertTrue(tools.compute_local_size_2d([512, 101]) == (19, 26))
        self.assertTrue(tools.compute_local_size_2d([512, 513]) == (1, 257))
        self.assertTrue(tools.compute_local_size_2d([300, 1025]) == (1, 342))
        self.assertTrue(tools.compute_local_size_2d([5120, 32]) == (16, 32))
        self.assertTrue(tools.compute_local_size_2d([5120011, 320001]) == (1, 512))
        self.assertTrue(tools.compute_local_size_2d([102, 7]) == (102, 4))
