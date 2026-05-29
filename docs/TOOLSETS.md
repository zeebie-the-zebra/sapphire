# Toolsets

Named groups of tools so you don't have to switch between memory and web for example, just use any tools you want in a set. Switch what abilities the AI has access to per-chat. Each of your personas may have a different toolset based on what they do.

## Usage

Use the UI to edit tool sets. Look in the sidebar for Toolset Editor. You can use the built-in toolsets (default, work, smarthome, personality), or create your own that combine tools from various files. 

## Quick switch

You can quick-switch the active toolset below the user chat input. Each chat/persona has its own toolset saved in the chat history file, so if you switch to another chat, it activates that toolset.

<img width="50%" alt="sapphire-toolsets" src="https://github.com/user-attachments/assets/a800437e-f571-4b13-9e15-9f221f56c96f" />


## Reference for AI

Toolsets are named groups of tools/functions the AI can access.

BUILT-IN TOOLSETS:
- default: Web search, memory, and general-purpose tools — the standard set
- work: Web, research, and productivity tools
- smarthome: Home Assistant control (scenes, lights, climate, areas)
- personality: Self-modification — prompt editing, prompt pieces, reset

HOW IT WORKS:
- Each chat stores its active toolset in chat history
- Switching chats switches toolsets automatically
- Toolset Editor in sidebar to create/edit custom sets

MANAGE TOOLSETS:
- UI: Sidebar > Toolset Editor
- Files: core/toolsets/toolsets.json (defaults), user/toolsets/toolsets.json (custom)
- Built-in defaults seed `user/toolsets/toolsets.json` on first run; your edits live in the user file thereafter

CREATE CUSTOM TOOLSET:
1. Open Toolset Editor in sidebar
2. Name your toolset
3. Check the functions you want included
4. Save - available immediately in Chat Settings dropdown

SWITCH ACTIVE TOOLSET:
- Chat Settings dropdown below input
- Or via ability tool if available
