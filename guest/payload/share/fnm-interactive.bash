# Loaded by the agent interactive Bash shell only.
FNM_PATH="$HOME/.local/share/fnm"
if [[ -x "$FNM_PATH/fnm" ]]; then
  export PATH="$FNM_PATH:$PATH"
  export FNM_DIR="$FNM_PATH"
  eval "$("$FNM_PATH/fnm" env --use-on-cd --shell bash)"
fi
