"""A tiny MCP server used to show that eval/run_eval.py can benchmark any MCP server, not just this one.

Four read-only tools over an in-memory notebook, with deliberately overlapping jobs
(list vs search, one note vs many, tags vs text), which is where tool descriptions matter.

  python eval/run_eval.py --server "python examples/notes_server.py" \\
      --questions examples/notes_questions.jsonl --domain "the user's notebook"
"""

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

NOTES = [
    {"id": 1, "title": "Trip to Lisbon", "tags": ["travel"], "folder": "personal",
     "text": "Book the tram tour and pastel de nata class."},
    {"id": 2, "title": "Sprint retro", "tags": ["work", "meeting"], "folder": "work",
     "text": "Deploys are slow; add caching to CI."},
    {"id": 3, "title": "Groceries", "tags": ["errands"], "folder": "personal",
     "text": "Oat milk, rice, lentils, coffee."},
    {"id": 4, "title": "Interview prep", "tags": ["work", "career"], "folder": "work",
     "text": "Practice system design: rate limiter, caching."},
    {"id": 5, "title": "Reading list", "tags": ["books"], "folder": "personal",
     "text": "Designing Data-Intensive Applications."},
]

server = MCPServer(name="notes-demo", version="1.0.0")


@server.tool(description="List notes, newest first. Optionally only one folder. Returns titles and ids, not the note text. "
                         "Do NOT use to find notes by a word or tag (use search_notes).")
def list_notes(folder: Annotated[Literal["all", "personal", "work"], Field(description="Folder to list, e.g. 'work'.")] = "all",
               limit: Annotated[int, Field(ge=1, le=20, description="How many notes, 1-20.")] = 10) -> dict:
    notes = [n for n in NOTES if folder == "all" or n["folder"] == folder][::-1][:limit]
    return {"notes": [{"id": n["id"], "title": n["title"]} for n in notes]}


@server.tool(description="Get one note's full text by its numeric id. Use when the user names a specific note.")
def get_note(note_id: Annotated[int, Field(description="The note's id, e.g. 3.")]) -> dict:
    for n in NOTES:
        if n["id"] == note_id:
            return n
    raise ToolError(f"No note with id {note_id}. Call list_notes to see the ids.")


@server.tool(description="Search note titles and text for a word or phrase. Use for 'find', 'which note mentions'. "
                         "Do NOT use to filter by tag (use notes_by_tag).")
def search_notes(query: Annotated[str, Field(description="Word or phrase, e.g. 'caching'.")]) -> dict:
    q = query.lower()
    return {"notes": [{"id": n["id"], "title": n["title"]} for n in NOTES if q in (n["title"] + " " + n["text"]).lower()]}


@server.tool(description="List notes that carry a tag, e.g. 'work' or 'travel'. Use when the user says 'tagged' or names a tag.")
def notes_by_tag(tag: Annotated[str, Field(description="Tag name without '#', e.g. 'travel'.")]) -> dict:
    return {"notes": [{"id": n["id"], "title": n["title"]} for n in NOTES if tag.lower().lstrip("#") in n["tags"]]}


if __name__ == "__main__":
    server.run()
