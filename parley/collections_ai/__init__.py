"""The model jobs for collections: drafting, the tone judge, reading replies and
updating the customer brief. Each job is one call with a typed, checked output.

Prompts live in prompts/ as versioned files ("draft_reminder.v1.md"). Changing a
prompt means adding a new version, so every stored output can say which prompt
produced it.
"""
