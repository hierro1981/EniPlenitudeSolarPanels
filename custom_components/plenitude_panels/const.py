"""Constants for the Plenitude Solar Panels integration."""

DOMAIN = "plenitude_panels"
PLATFORMS = ["sensor"]

CONF_COOKIE = "cookie"
CONF_USER_AGENT = "user_agent"
CONF_ACCOUNT_ID = "account_id"
CONF_SUPPLY_ID = "supply_id"
CONF_POLL_INTERVAL = "poll_interval"
CONF_POD = "pod"
CONF_RECOVERY_ENABLED = "recovery_enabled"

DEFAULT_POLL_INTERVAL = 5
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/156.0.0.0 Safari/537.36"
)
MIN_POLL_INTERVAL = 5
FIVE_MINUTE_HOUR_FRACTION = 5 / 60

BASE_URL = "https://eniplenitude.com"
