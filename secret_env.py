import os


def load_secret(name: str) -> str:
    """Read from the environment, falling back to the Windows user registry for shells opened before it was set."""
    if value := os.environ.get(name):
        return value
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg:
                return winreg.QueryValueEx(reg, name)[0]
        except FileNotFoundError:
            pass
    raise SystemExit(f"{name} is not set.")
