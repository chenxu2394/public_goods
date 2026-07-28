import os


# Tests set every runtime value explicitly and must not inherit a developer's .env.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
