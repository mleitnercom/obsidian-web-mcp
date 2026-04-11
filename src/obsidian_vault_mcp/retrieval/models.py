"""Data models for semantic retrieval."""

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class Chunk:
    """A searchable text chunk derived from a markdown note."""

    id: str
    path: str
    title: str
    section: str
    text: str
    tokens: list[str]

    def to_dict(self) -> dict:
        """Serialize chunk metadata for cache persistence."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Chunk":
        """Rehydrate a chunk from cached JSON."""
        return cls(
            id=data["id"],
            path=data["path"],
            title=data.get("title", ""),
            section=data.get("section", ""),
            text=data["text"],
            tokens=list(data.get("tokens", [])),
        )

