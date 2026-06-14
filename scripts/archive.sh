#!/usr/bin/env bash
# archive.sh — Archiviert und stellt Instanzdaten einer oparl-bridge-Installation wieder her.
#
# Liest den Instanznamen aus .env (OPARL_BODY_NAME).
# Archiviert: Datenbank, Wikidata-Cache, Session-Cookies, .env-Konfiguration.
#
# Verwendung:
#   scripts/archive.sh [--delete] [--output-dir DIR]
#   scripts/archive.sh -d -o /mnt/backup
#   scripts/archive.sh restore [--output-dir DIR]

set -euo pipefail

# ---------------------------------------------------------------------------
# Argumente
# ---------------------------------------------------------------------------
MODE="archive"
DELETE=0
OUTPUT_DIR="."

while [[ $# -gt 0 ]]; do
    case "$1" in
        restore)           MODE="restore";     shift ;;
        -d|--delete)       DELETE=1;           shift ;;
        -o|--output-dir)   OUTPUT_DIR="$2";    shift 2 ;;
        -h|--help)
            sed -n '/^# /s/^# \?//p' "$0"
            exit 0
            ;;
        *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Projektverzeichnis (Skript liegt in scripts/, Daten in ../)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Fehler: .env nicht gefunden in $PROJECT_DIR" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Instanzname aus .env lesen
# ---------------------------------------------------------------------------
_env_value() {
    # Liest einen Wert aus .env, ignoriert Kommentare und Leerzeilen
    local key="$1"
    grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^["'"'"']//; s/["'"'"']$//' || true
}

DB_URL="$(_env_value OPARL_DATABASE_URL)"
DB_URL="${DB_URL:-sqlite:///./oparl_bridge.db}"
DB_FILE="${DB_URL#sqlite:///}"
DB_FILE="${DB_FILE#./}"
DB_PATH="$PROJECT_DIR/$DB_FILE"

# ---------------------------------------------------------------------------
# restore-Modus
# ---------------------------------------------------------------------------
if [[ "$MODE" == "restore" ]]; then
    SEARCH_DIR="$(realpath "$OUTPUT_DIR")"

    # Archive suchen (neueste zuerst)
    mapfile -t ARCHIVES < <(ls -t "$SEARCH_DIR"/oparl-bridge-*.tar.gz 2>/dev/null || true)

    if [[ ${#ARCHIVES[@]} -eq 0 ]]; then
        echo "Keine Archive gefunden in: $SEARCH_DIR" >&2
        exit 1
    fi

    echo "Verfügbare Archive in $SEARCH_DIR:"
    echo ""
    for i in "${!ARCHIVES[@]}"; do
        f="${ARCHIVES[$i]}"
        size="$(du -sh "$f" 2>/dev/null | cut -f1)"
        mtime="$(stat -c '%y' "$f" 2>/dev/null | cut -d. -f1)"
        printf "  [%2d]  %-50s  %s  %s\n" "$((i+1))" "$(basename "$f")" "$size" "$mtime"
    done

    echo ""
    read -rp "Archiv auswählen [1-${#ARCHIVES[@]}]: " CHOICE

    if ! [[ "$CHOICE" =~ ^[0-9]+$ ]] || [[ "$CHOICE" -lt 1 ]] || [[ "$CHOICE" -gt ${#ARCHIVES[@]} ]]; then
        echo "Ungültige Auswahl." >&2
        exit 1
    fi

    SELECTED="${ARCHIVES[$((CHOICE-1))]}"

    echo ""
    echo "Ausgewählt: $(basename "$SELECTED")"
    echo ""
    echo "Folgende Dateien werden in $PROJECT_DIR überschrieben:"
    tar -tzf "$SELECTED" | while IFS= read -r f; do
        target="$PROJECT_DIR/$f"
        if [[ -f "$target" ]]; then
            size="$(du -sh "$target" 2>/dev/null | cut -f1)"
            printf "  %-40s  (vorhanden, %s)\n" "$f" "$size"
        else
            printf "  %-40s  (neu)\n" "$f"
        fi
    done

    echo ""
    if ss -tlnp 2>/dev/null | grep -q ':8000 ' || lsof -ti :8000 &>/dev/null 2>&1; then
        echo "WARNUNG: oparl-bridge scheint auf Port 8000 zu laufen."
        echo "         Bitte Server stoppen, bevor du wiederherstellst."
        echo ""
        read -rp "Trotzdem fortfahren? (j/N): " FORCE
        if [[ "$FORCE" != "j" && "$FORCE" != "J" ]]; then
            echo "Abgebrochen." >&2
            exit 1
        fi
        echo ""
    fi

    read -rp "Wirklich wiederherstellen? Aktuelle Daten werden überschrieben. (j/N): " CONFIRM
    if [[ "$CONFIRM" != "j" && "$CONFIRM" != "J" ]]; then
        echo "Abgebrochen." >&2
        exit 0
    fi

    echo ""
    tar -xzf "$SELECTED" -C "$PROJECT_DIR"
    echo "✓  Wiederhergestellt: $(basename "$SELECTED")"
    exit 0
fi

# ---------------------------------------------------------------------------
# archive-Modus (Standard)
# ---------------------------------------------------------------------------
BODY_NAME="$(_env_value OPARL_BODY_NAME)"

if [[ -z "$BODY_NAME" ]]; then
    echo "Warnung: OPARL_BODY_NAME nicht in .env gesetzt — verwende 'instance'" >&2
    BODY_NAME="instance"
fi

INSTANCE_SLUG="$(echo "$BODY_NAME" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9äöüß]/-/g; s/--*/-/g; s/^-//; s/-$//')"
DATE="$(date +%Y-%m-%d)"
ARCHIVE_NAME="oparl-bridge-${INSTANCE_SLUG}-${DATE}.tar.gz"
ARCHIVE_PATH="$(realpath "$OUTPUT_DIR")/$ARCHIVE_NAME"

# Nicht überschreiben — Zähler anhängen falls Datei schon existiert
if [[ -f "$ARCHIVE_PATH" ]]; then
    _n=2
    while [[ -f "$(realpath "$OUTPUT_DIR")/oparl-bridge-${INSTANCE_SLUG}-${DATE}_${_n}.tar.gz" ]]; do
        (( _n++ ))
    done
    ARCHIVE_NAME="oparl-bridge-${INSTANCE_SLUG}-${DATE}_${_n}.tar.gz"
    ARCHIVE_PATH="$(realpath "$OUTPUT_DIR")/$ARCHIVE_NAME"
    echo "Hinweis: Archiv für heute existiert bereits — speichere als $ARCHIVE_NAME" >&2
fi

# ---------------------------------------------------------------------------
# Zu archivierende Dateien bestimmen
# ---------------------------------------------------------------------------
FILES_TO_ARCHIVE=()

add_if_exists() {
    local path="$1"
    if [[ -f "$path" ]]; then
        FILES_TO_ARCHIVE+=("$path")
    else
        echo "  Übersprungen (nicht vorhanden): $path" >&2
    fi
}

add_if_exists "$DB_PATH"
add_if_exists "${DB_PATH}-wal"
add_if_exists "${DB_PATH}-shm"
add_if_exists "$PROJECT_DIR/wikidata_cache.json"
add_if_exists "$PROJECT_DIR/oparl_cookies.json"
add_if_exists "$ENV_FILE"

if [[ ${#FILES_TO_ARCHIVE[@]} -eq 0 ]]; then
    echo "Fehler: Keine Dateien zum Archivieren gefunden." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Ausgabeverzeichnis anlegen
# ---------------------------------------------------------------------------
mkdir -p "$OUTPUT_DIR"

# ---------------------------------------------------------------------------
# Warnung bei laufender Instanz (Port 8000)
# ---------------------------------------------------------------------------
if ss -tlnp 2>/dev/null | grep -q ':8000 ' || lsof -ti :8000 &>/dev/null 2>&1; then
    echo "Warnung: oparl-bridge scheint auf Port 8000 zu laufen." >&2
    echo "         Die Datenbank wird trotzdem archiviert (SQLite WAL ist sicher für Lesezugriffe)." >&2
    echo "         Für einen konsistenten Snapshot: Server vorher stoppen." >&2
fi

# ---------------------------------------------------------------------------
# Archiv erstellen (relative Pfade ab PROJECT_DIR)
# ---------------------------------------------------------------------------
echo "Instanz:  $BODY_NAME"
echo "Archiv:   $ARCHIVE_PATH"
echo "Dateien:"
for f in "${FILES_TO_ARCHIVE[@]}"; do
    printf "  %s  (%s)\n" "$f" "$(du -sh "$f" 2>/dev/null | cut -f1)"
done

REL_FILES=()
for f in "${FILES_TO_ARCHIVE[@]}"; do
    REL_FILES+=("$(realpath --relative-to="$PROJECT_DIR" "$f")")
done

tar -czf "$ARCHIVE_PATH" -C "$PROJECT_DIR" "${REL_FILES[@]}"

ARCHIVE_SIZE="$(du -sh "$ARCHIVE_PATH" | cut -f1)"
echo ""
echo "✓  Archiv erstellt: $ARCHIVE_PATH  ($ARCHIVE_SIZE)"

# ---------------------------------------------------------------------------
# Optional: Quelldateien löschen (nicht .env — Konfiguration immer behalten)
# ---------------------------------------------------------------------------
if [[ $DELETE -eq 1 ]]; then
    echo ""
    echo "Lösche Quelldateien (außer .env) ..."
    for f in "${FILES_TO_ARCHIVE[@]}"; do
        if [[ "$f" == "$ENV_FILE" ]]; then
            echo "  Behalten (Konfiguration): $f"
            continue
        fi
        rm -f "$f"
        echo "  Gelöscht: $f"
    done
    # WAL/SHM werden von SQLite implizit angelegt — auch löschen wenn nicht explizit archiviert
    for suffix in -wal -shm; do
        extra="${DB_PATH}${suffix}"
        if [[ -f "$extra" ]]; then
            rm -f "$extra"
            echo "  Gelöscht: $extra"
        fi
    done
    echo "✓  Fertig."
fi
