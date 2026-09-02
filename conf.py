# See the Documenteer docs for how to customize conf.py:
# https://documenteer.lsst.io/technotes/

from documenteer.conf.technote import *  # noqa F401 F403

extensions.append("sphinxcontrib.mermaid")  # noqa F405

html_static_path.append("_static")  # noqa F405
html_css_files.append("mermaid.css")  # noqa F405
