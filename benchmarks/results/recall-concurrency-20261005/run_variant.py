"""Run the same probe against an archived implementation, without editing production."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
source = Path(sys.argv.pop(1))
spec = importlib.util.spec_from_file_location('recall_variant', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from scripts.benchmark import probe_recall_stages as probe  # noqa: E402

probe.recall_with_kg_fusion = module.recall_with_kg_fusion
raise SystemExit(probe.main())
