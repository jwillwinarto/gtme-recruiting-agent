#!/bin/zsh
# Double-click in Finder to start the job digest UI (opens http://localhost:8501).
# Close this Terminal window (or press Ctrl+C) to stop it.

cd "${0:A:h}" || exit 1

# Finder-launched scripts don't inherit keys exported in another terminal.
# Ask for any that are missing; they live only in this window, never on disk.
for var in ANTHROPIC_API_KEY APOLLO_API_KEY; do
  if [[ -z "${(P)var}" ]]; then
    read -rs "val?$var not set. Paste it (or press Enter to skip): "
    echo
    [[ -n "$val" ]] && export "$var=$val"
  fi
done

exec ./.venv/bin/streamlit run app.py
