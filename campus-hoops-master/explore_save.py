"""
Run this script in PyCharm to get an initial map of the session.json structure.
Adjust SAVE_FOLDER to point at whichever export you want to inspect.
"""

from save_loader import SaveFile

SAVE_FOLDER = "saint_francis_pa_season_6_2026-05-10T150915705711"

save = SaveFile(SAVE_FOLDER)

# 1. Print all top-level keys with types/sizes
print("=" * 60)
print("TOP-LEVEL KEYS")
print("=" * 60)
save.top_level_keys()

# 2. Deep-dive into first 3 levels of structure
print()
print("=" * 60)
print("FULL STRUCTURE (depth=3)")
print("=" * 60)
save.describe(max_depth=3)

# After this runs, drill into specific areas like:
#   save.describe("teams", max_depth=4)
#   save.keys("teams")
#   save.get("teams.saint_francis_pa")
#   db = save.history_db()
#   db.execute("SELECT * FROM ...").fetchall()
