"""
Copyright (c) 2021-, James Vecellio, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from cereal import car, log
from openpilot.common.constants import CV


class NavigationDesires:
  def __init__(self):
    self.desire = log.Desire.none
    self._turn_speed_limit = 20 * CV.MPH_TO_MS

  def update(self, CS: car.CarState, lateral_active: bool) -> log.Desire:
    # TEMPORARILY DISABLED - debugging engagement issue
    return log.Desire.none
