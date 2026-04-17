import os


class Settings:
    def __init__(self) -> None:
        origins = os.getenv("CORS_ORIGINS", "*")
        if origins.strip() == "*":
            self.cors_origins = ["*"]
        else:
            self.cors_origins = [origin.strip() for origin in origins.split(",") if origin.strip()]


settings = Settings()
