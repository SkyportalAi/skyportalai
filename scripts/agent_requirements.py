"""Print the agent's requirements: the base minus the CLI's packages, plus the agent extra.

CI installs these with the package itself installed `--no-deps`, so the agent runs
and is tested with none of the CLI's packages present (#3657). Until the base drops
the CLI's packages, `pip install "skyportalai[agent]"` alone still pulls them in.
"""

import pathlib
import re
import tomllib

project = tomllib.loads((pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]


def name(requirement: str) -> str:
    return re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0].lower().replace("_", "-")


cli = {name(r) for r in project["optional-dependencies"]["cli"]}
agent = project["optional-dependencies"]["agent"]
keep = [r for r in project["dependencies"] if name(r) not in cli or name(r) in {name(a) for a in agent}]
print("\n".join(dict.fromkeys(keep + agent)))
