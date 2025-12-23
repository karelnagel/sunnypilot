"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from __future__ import annotations

import math
import time
import urllib.request
import json
from dataclasses import dataclass

from openpilot.sunnypilot.navd.helpers import Coordinate

OVERPASS_API_URL = "https://overpass-api.de/api/interpreter"
CACHE_DURATION_SECONDS = 300  # 5 minutes
SEARCH_RADIUS_METERS = 5000  # 5km radius


@dataclass
class SpeedCamera:
  latitude: float
  longitude: float
  speed_limit: int  # 0 if unknown

  def to_coordinate(self) -> Coordinate:
    return Coordinate(self.latitude, self.longitude)


class RoadAlerts:
  def __init__(self):
    self._cache: list[SpeedCamera] = []
    self._cache_center: Coordinate | None = None
    self._cache_time: float = 0

  def _should_refresh_cache(self, pos: Coordinate) -> bool:
    if not self._cache_center:
      return True
    if time.monotonic() - self._cache_time > CACHE_DURATION_SECONDS:
      return True
    # Refresh if moved more than half the search radius from cache center
    if self._cache_center.distance_to(pos) > SEARCH_RADIUS_METERS / 2:
      return True
    return False

  def _fetch_speed_cameras(self, lat: float, lon: float) -> list[SpeedCamera]:
    query = f"""
    [out:json][timeout:10];
    (
      node["highway"="speed_camera"](around:{SEARCH_RADIUS_METERS},{lat},{lon});
      node["enforcement"="maxspeed"](around:{SEARCH_RADIUS_METERS},{lat},{lon});
    );
    out body;
    """

    try:
      data = urllib.parse.urlencode({"data": query}).encode("utf-8")
      req = urllib.request.Request(OVERPASS_API_URL, data=data, method="POST")
      req.add_header("User-Agent", "sunnypilot/1.0")

      with urllib.request.urlopen(req, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))

      cameras = []
      for element in result.get("elements", []):
        if element.get("type") == "node":
          tags = element.get("tags", {})
          speed_limit = 0
          if "maxspeed" in tags:
            try:
              speed_limit = int(tags["maxspeed"].split()[0])
            except (ValueError, IndexError):
              pass
          cameras.append(SpeedCamera(
            latitude=element["lat"],
            longitude=element["lon"],
            speed_limit=speed_limit
          ))
      return cameras
    except Exception:
      return []

  def get_nearby_alerts(self, pos: Coordinate, bearing: float, max_distance: float = 2000) -> list[dict]:
    """Get speed cameras ahead of vehicle within max_distance meters."""
    if self._should_refresh_cache(pos):
      self._cache = self._fetch_speed_cameras(pos.latitude, pos.longitude)
      self._cache_center = pos
      self._cache_time = time.monotonic()

    alerts = []
    for camera in self._cache:
      camera_pos = camera.to_coordinate()
      distance = pos.distance_to(camera_pos)

      if distance > max_distance:
        continue

      # Check if camera is ahead (within 90 degrees of bearing)
      bearing_to_camera = self._bearing_to(pos, camera_pos)
      angle_diff = abs((bearing_to_camera - bearing + 180) % 360 - 180)
      if angle_diff > 90:
        continue

      alerts.append({
        "type": "speedCamera",
        "distance": distance,
        "speedLimit": camera.speed_limit
      })

    # Sort by distance, closest first
    alerts.sort(key=lambda x: x["distance"])
    return alerts[:2]  # Return max 2 alerts

  def _bearing_to(self, from_pos: Coordinate, to_pos: Coordinate) -> float:
    lat1 = math.radians(from_pos.latitude)
    lat2 = math.radians(to_pos.latitude)
    dlon = math.radians(to_pos.longitude - from_pos.longitude)

    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360
