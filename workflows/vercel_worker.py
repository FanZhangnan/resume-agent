"""Private ASGI adapter for Vercel's GA service queue trigger."""

# Importing the workflow registry registers both durable workflow and step
# consumers with vercel.workers before the ASGI callback app is created.
import workflows.resume_workflow as _resume_workflow  # noqa: F401
from vercel.workers import get_asgi_app


app = get_asgi_app()
