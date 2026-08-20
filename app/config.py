from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    google_cloud_project: str | None
    google_cloud_location: str
    gemini_model: str
    firestore_database: str
    use_in_memory_store: bool

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            google_cloud_location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            firestore_database=os.getenv("FIRESTORE_DATABASE", "(default)"),
            use_in_memory_store=os.getenv("USE_IN_MEMORY_STORE", "false").lower() == "true",
        )
