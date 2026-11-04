import os
import sys
from pathlib import Path

os.environ.setdefault("SEC_USER_AGENT", "FinRobot-tests")
# tracing backends must stay off in tests, whatever the developer's .env says
for var in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
    os.environ[var] = ""
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
sys.path.insert(0, str(Path(__file__).parent))
