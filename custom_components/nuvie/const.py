"""Constants for the Nuvie Planner integration."""

DOMAIN = "nuvie"

# the export's files live in <config>/nuvie/ (unzip the Nuvie export into the config folder)
DATA_DIR = "nuvie"
MANIFEST_FILE = "nuvie_home.json"
MODEL_FILE = "model.glb"
SCHEMA_PREFIX = "nuvie.home-assistant/1"

CARD_FILE = "nuvie-home-card.js"
CARD_URL = "/nuvie_static/nuvie-home-card.js"

CONF_SYNC_AREAS = "sync_areas"
CONF_ASSIGN_ENTITIES = "assign_entities"

SERVICE_SYNC = "sync"
STORAGE_KEY = "nuvie.sync"
LINKS_STORAGE_KEY = "nuvie.links"
STORAGE_VERSION = 1

# where the plan comes from: Nuvie itself (paired with an account) or an export unzipped into <config>/
CONF_SOURCE = "source"
SOURCE_CLOUD = "cloud"
SOURCE_FILES = "files"
CONF_URL = "url"
CONF_TOKEN = "token"
CONF_PROJECT_ID = "project_id"
DEFAULT_URL = "https://planner.nuvie.in"

# a paired Home Assistant checks Nuvie for a newer plan this often (nuvie.sync checks now)
UPDATE_MINUTES = 10
CLOUD_STORAGE_KEY = "nuvie.cloud"
