import pytest
from unittest.mock import patch
from eo_tools.S1.core import S1IWSwath
from eo_tools.S1.core import range_doppler
import numpy as np


@pytest.fixture
def create_swath():
    safe_dir = "./data/S1/S1A_IW_SLC__1SDV_20230904T063730_20230904T063757_050174_0609E3_DAA1.SAFE"
    iw = 1
    pol = "vh"
    return S1IWSwath(safe_dir, iw, pol)


def test_s1iwswath_init(create_swath):
    import os

    swath = create_swath

    assert os.path.isfile(swath.pth_tiff)
    assert swath.start_time == "2023-09-04T06:37:31.072288"
    assert swath.lines_per_burst == 1507
    assert swath.samples_per_burst == 23055
    assert swath.burst_count == 9
    assert swath.beta_nought == 2.370000e02

# Test case for beta calibration using create_swath fixture
def test_calibration_factor_beta(create_swath):
    swath = create_swath
    
    # Mock the necessary attributes
    with patch.object(swath, 'beta_nought', 1.5):
        # Act: Call the calibration_factor method with cal_type "beta"
        result = swath.calibration_factor(cal_type="beta")
        
        # Assert: The result should match the beta_nought constant
        assert result == 1.5, "Beta calibration factor should be a constant"

# Test case for sigma calibration with interpolation using create_swath fixture
def test_calibration_factor_sigma(create_swath):
    swath = create_swath

    # Mock the necessary attributes
    with patch.object(swath, 'lines_per_burst', 2), \
         patch.object(swath, 'samples_per_burst', 3), \
         patch.object(swath, 'calvec', [
             {"line": "0", "pixel": {"#text": "0 1 2"}, "sigmaNought": {"#text": "1.0 2.0 3.0"}},
             {"line": "1", "pixel": {"#text": "0 1 2"}, "sigmaNought": {"#text": "4.0 5.0 6.0"}}
         ]):
        # Act: Call the calibration_factor method with cal_type "sigma"
        result = swath.calibration_factor(cal_type="sigma")
        
        # Assert: Compare the result to the expected interpolation result
        expected_result = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
        assert np.allclose(result, expected_result), "Sigma nought calibration factor interpolation failed"



def test_range_doppler():
    xx = np.array([0.0, 5.0])
    yy = np.array([0.0, 0.0])
    zz = np.array([0.0, 5.0])

    positions = np.vstack((np.linspace(-10, 10, 10), np.full(10, 0), np.full(10, 10))).T

    velocities = np.vstack((np.ones(10), np.zeros(10), np.zeros(10))).T

    expected_i_zd = np.array([4.5, 6.75])
    expected_r_zd = np.array([10, 5])

    i_zd, r_zd = range_doppler(xx, yy, zz, positions, velocities)

    np.testing.assert_allclose(i_zd, expected_i_zd, rtol=1e-5, atol=1e-8)
    np.testing.assert_allclose(r_zd, expected_r_zd, rtol=1e-5, atol=1e-8)
