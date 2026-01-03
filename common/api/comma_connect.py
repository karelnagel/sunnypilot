import os

from openpilot.common.api.base import BaseApi
from openpilot.common.params import Params

API_HOST = os.getenv('API_HOST', Params().get("APIHost", encoding='utf-8'))


class CommaConnectApi(BaseApi):
  def __init__(self, dongle_id):
    super().__init__(dongle_id, API_HOST)
    self.user_agent = "openpilot-"
