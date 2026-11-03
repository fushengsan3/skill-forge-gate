"""Distribution identity, selected at build time; there is no runtime mode switch."""
ID = "full"


def info():
    coreless = ID == "coreless"
    return {"id": ID, "name": "无核版" if coreless else "完整版",
            "security_checks": not coreless}
