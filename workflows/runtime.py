"""Single shared Vercel Workflows registry.

The registry object must be created exactly once and imported everywhere so that
``module + qualname`` of every decorated step and workflow stays stable; those
identifiers form the persisted workflow and step ids. In offline tests
``AGENT_WORKFLOW_TEST=1`` builds the registry with ``as_vercel_job=False``.
"""

import os

from vercel.workflow import Workflows

wf = Workflows(as_vercel_job=os.environ.get("AGENT_WORKFLOW_TEST") != "1")
