import unittest
from nose.tools import assert_list_equal
import numpy
import numpy.testing
from stencil_code.library.diagnostic_stencil import DiagnosticStencil
from stencil_code.neighborhood import Neighborhood

from stencil_code.stencil_kernel2 import Stencil


class TestBoundaryHandling(unittest.TestCase):
    def test_python_clamping(self):
        class Clamper(Stencil):
            neighborhoods = [Neighborhood.von_neuman_neighborhood(radius=1, dim=2)]

            def kernel(self, in_grid, out_grid):
                for p in self.interior_points(out_grid):
                    for n in self.neighbors(p, 0):
                        out_grid[p] += in_grid[n]

        clamper = Clamper(backend='python', boundary_handling='clamp')
        in_grid = numpy.ones([10, 10])

        clamper.current_shape = in_grid.shape   # this is ordinarily set by interior_points_loop

        assert_list_equal(
            [x for x in clamper.neighbors((9, 9), 0)],
            [(8, 9), (9, 8), (9, 9), (9, 9), (9, 9)],
            "clamping around (9, 9) should keep all values between 0 and 9 inclusive"
        )
        assert_list_equal(
            [x for x in clamper.neighbors((0, 0), 0)],
            [(0, 0), (0, 0), (0, 0), (0, 1), (1, 0)],
            "clamping around (0, 0) should keep all between 0 and 9 inclusive"
        )
        assert_list_equal(
            [x for x in clamper.neighbors((0, 0), 0)],
            [(0, 0), (0, 0), (0, 0), (0, 1), (1, 0)],
            "clamping around (0, 9) should keep all between 0 and 9 inclusive"
        )
        assert_list_equal(
            [x for x in clamper.neighbors((3, 3), 0)],
            [(2, 3), (3, 2), (3, 3), (3, 4), (4, 3)],
            "neighborhoods around interior points are not clamped"
        )

    def test_clamped(self):
        in_grid = numpy.ones([10, 10])

        python_clamped_kernel = DiagnosticStencil(backend='python', boundary_handling='clamp')
        c_clamped_kernel = DiagnosticStencil(backend='python', boundary_handling='clamp')
        python_clamped_out = python_clamped_kernel(in_grid)
        c_clamped_out = c_clamped_kernel(in_grid)

        numpy.testing.assert_array_almost_equal(python_clamped_out, c_clamped_out, decimal=4)
        self.assertTrue(python_clamped_out[0, 0] == 30)

        python_unclamped_kernel = DiagnosticStencil(backend='python', boundary_handling='zero')
        c_unclamped_kernel = DiagnosticStencil(backend='python', boundary_handling='zero')
        python_unclamped_out = python_unclamped_kernel(in_grid)
        c_unclamped_out = c_unclamped_kernel(in_grid)

        # print(python_unclamped_out)
        numpy.testing.assert_array_almost_equal(python_unclamped_out, c_unclamped_out, decimal=4)
        self.assertTrue(python_unclamped_out[0, 0] == 0)
