"""Load the coordinator embedded in the standalone Bash script for testing."""
from pathlib import Path
import sys
import types

script = (Path(__file__).resolve().parents[1] / 'compile_deploy.sh').read_text()
source = script.split("cat <<'GUARDIAN_DEPLOY_PY'\n", 1)[1].split('\nGUARDIAN_DEPLOY_PY\n', 1)[0]
deploy_remote = types.ModuleType('deploy_remote')
sys.modules['deploy_remote'] = deploy_remote
exec(compile(source, '<embedded deployment coordinator>', 'exec'), deploy_remote.__dict__)
