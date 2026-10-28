"""Load the packaged native demo configuration without mutating its YAML."""
from pathlib import Path

from chain.configuration import load_configuration

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/minimum"
EPISODE = ROOT / "demo/episodes/stroke_reference_001"


def load_package(*, fixture_profile="positive", clinical_path=None, deployment_path=None):
    if fixture_profile not in {"positive", "negative"}:
        raise ValueError("FACT_DEMO_FIXTURE_PROFILE_UNSUPPORTED")
    suffix = "" if fixture_profile == "positive" else "-negative"
    return load_configuration(clinical_path or CONFIG / "clinical.yaml",
        site_path=CONFIG / "site.yaml", safety_path=CONFIG / "safety.yaml",
        deployment_path=deployment_path or CONFIG / ("deployment" + suffix + ".yaml"),
        metadata_path=CONFIG / "module_metadata.json",
        installations=CONFIG / ("installed" + suffix + ".json"), installation_root=ROOT)
