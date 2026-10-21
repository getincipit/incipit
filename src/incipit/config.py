from dataclasses import dataclass

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# The drafting model and the one asked when it will not produce a usable reply.
DEFAULT_OPENROUTER_MODEL = "qwen/qwen3.7-plus"
DEFAULT_OPENROUTER_FALLBACK_MODEL = "qwen/qwen3.6-35b-a3b"

# All-time model spend cap in dollars, shared by every cost-generating step.
DEFAULT_MAX_TOTAL_COST = 2.0

# How a draft transcription is produced.
PATH_SINGLE_PASS = "single_pass"

# What a new site calls itself until its owner names it; the name then lives in site_settings.
DEFAULT_PRODUCT_NAME = "Incipit"


@dataclass
class Settings:
    db_path: str
    bucket_uri: str = ""
    http_timeout: float = 30.0
    http_retries: int = 3
    openrouter_api_key: str = ""
    openrouter_base_url: str = OPENROUTER_BASE_URL
    openrouter_model: str = DEFAULT_OPENROUTER_MODEL
    openrouter_fallback_model: str = DEFAULT_OPENROUTER_FALLBACK_MODEL
    openrouter_timeout: float = 120.0
    openrouter_retries: int = 3
    # Sent as HTTP-Referer / X-Title so a gateway can attribute the calls; blank means not sent.
    http_referer: str = ""
    http_title: str = ""
    max_total_cost: float = DEFAULT_MAX_TOTAL_COST
    # None: no monthly cap, only the all-time one (app_settings.monthly_cap).
    monthly_cap: float | None = None
    # Where the site lives when run from a data folder (incipit.toml), and how it was bound.
    data_folder: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    # Announced on the network as {site_name}.local (localname.py); empty announces nothing.
    site_name: str = ""
    # Opened on the router by UPnP while the site runs (portmap.py).
    forward_port: bool = False
    # The same site over HTTPS, with the certificate the site makes itself in tls_dir (tls.py).
    https_port: int = 0
    tls_dir: str = ""
    # A Cloudflare quick tunnel: "off", "auto" (when the router gives no internet address) or "on" (tunnel.py).
    tunnel: str = "off"
    tunnel_dir: str = ""
    # A Telegraph page that always leads to the tunnel's current address (fixedlink.py).
    fixed_link: bool = True
