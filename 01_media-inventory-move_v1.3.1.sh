#!/bin/bash

# --- Color Definitions ---
RED='\033[0;31m'
GREEN='\033[0;32m'
BOLD_GREEN='\033[1;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Reset terminal color immediately
printf "${NC}"

# --- Flag Handling ---
IGNORE_SRC_FAIL=false
if [[ "$1" == "-i" ]]; then
    IGNORE_SRC_FAIL=true
    shift 
fi

SOURCE_DIR="$1"
DEST_DIR="$2"

if [[ -z "$SOURCE_DIR" || -z "$DEST_DIR" ]]; then
    echo -e "${RED}Usage: $0 [-i] [source_directory] [destination_directory]${NC}"
    exit 1
fi

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
DIR_NAME=$(basename "$SOURCE_DIR")
FILE_PREFIX="${DIR_NAME// /_}"
CSV_OUT="${FILE_PREFIX}_inventory_${TIMESTAMP}.csv"
LOG_FILE="${FILE_PREFIX}_process_${TIMESTAMP}.log"

# --- CLEAN LOGGING SETUP ---
exec > >(tee >(sed 's/\x1b\[[0-9;]*m//g' > "$LOG_FILE") ) 2>&1

echo "========================================================================="
echo "PROCESS START: $(date)"
echo "SOURCE:        $SOURCE_DIR"
echo "DESTINATION:   $DEST_DIR"
echo "========================================================================="

# --- PRE-FLIGHT STORAGE CHECK ---
echo -e "\n${YELLOW}[PRE-CHECK] CHECKING DISK SPACE${NC}"
echo "-------------------------------------------------------------------------"
SRC_KB=$(du -sk "$SOURCE_DIR" | awk '{print $1}')
DEST_AVAIL_KB=$(df -k "$DEST_DIR" | tail -1 | awk '{if (NF==1) {getline; print $3} else {print $4}}')

if [ "$SRC_KB" -gt "$DEST_AVAIL_KB" ]; then
    SRC_HUMAN=$(du -sh "$SOURCE_DIR" | awk '{print $1}')
    DEST_HUMAN=$(df -h "$DEST_DIR" | tail -1 | awk '{if (NF==1) {getline; print $3} else {print $4}}')
    echo -e "${RED}FAIL: Destination may not have enough space!${NC}"
    echo "Source requires approx: $SRC_HUMAN"
    echo "Destination available:  $DEST_HUMAN"
    if [ "$IGNORE_SRC_FAIL" = false ]; then
        read -p "Do you want to proceed anyway? (y/n) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Process aborted by user."
            exit 1
        fi
    fi
else
    echo "Disk space check passed."
fi

# 1. GENERATE CSV & MD5s
echo -e "\n${YELLOW}[STEP 1] GENERATING INVENTORY & MD5 SIDE_CARS${NC}"
echo "-------------------------------------------------------------------------"
echo "Path,Filename" > "$CSV_OUT"

find "$SOURCE_DIR" -type f \( \
    -iname "*.mp4" -o -iname "*.mkv" -o -iname "*.avi" -o \
    -iname "*.mpg" -o -iname "*.mpeg" -o -iname "*.mov" -o \
    -iname "*.m4v" -o -iname "*.flv" -o -iname "*.wmv" -o \
    -iname "*.wav" -o -iname "*.m4a" -o -iname "*.mp3" -o \
    -iname "*.jp2" -o -iname "*.tif" -o -iname "*.tiff" -o \
    -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o \
    -iname "*.txt" -o -iname "*.docx" -o -iname "*.xlsx" -o \
    -iname "*.pdf" -o -iname "*.zip" -o -iname "*.json" -o \
    -iname "*.xml" -o -iname "*.vtt" -o -iname "*.framemd5" \
    \) ! -path '*/.*' -print0 | while IFS= read -r -d '' FILE; do
    
    FILE_DIR=$(dirname "$FILE")
    FILE_NAME=$(basename "$FILE")
    MD5_FILE="$FILE.md5"
    echo "\"${FILE_DIR//\"/\"\"}\",\"${FILE_NAME//\"/\"\"}\"" >> "$CSV_OUT"

    if [[ ! -f "$MD5_FILE" ]]; then
        printf "MD5 Check: %-60s [ ${YELLOW}NEW GEN${NC} ]\n" "$FILE_NAME"
        if command -v md5sum >/dev/null 2>&1; then
            md5sum -b -- "$FILE" > "$MD5_FILE"
        else
            md5 -r -- "$FILE" > "$MD5_FILE"
        fi
    else
        printf "MD5 Check: %-60s [ ${GREEN}EXISTS${NC} ]\n" "$FILE_NAME"
    fi
done

# 2. SOURCE PRE-VERIFICATION
echo -e "\n${YELLOW}[STEP 2] SOURCE PRE-VERIFICATION${NC}"
echo "-------------------------------------------------------------------------"
src_fail_count=0
while IFS= read -r -d '' SRC_MD5; do
    SRC_MEDIA_FILE="${SRC_MD5%.md5}"
    [[ "$(basename "$SRC_MD5")" == .* ]] && continue
    if [[ ! -f "$SRC_MEDIA_FILE" ]]; then continue; fi

    expected_hash=$(grep -oE '[a-fA-F0-9]{32}' "$SRC_MD5" | head -n 1 | tr '[:upper:]' '[:lower:]')
    actual_hash=$(command -v md5sum >/dev/null 2>&1 && md5sum -- "$SRC_MEDIA_FILE" | grep -oE '[a-fA-F0-9]{32}' | head -n 1 | tr '[:upper:]' '[:lower:]' || md5 -q -- "$SRC_MEDIA_FILE" | tr '[:upper:]' '[:lower:]')

    if [ "$actual_hash" != "$expected_hash" ]; then
        printf "Source Verify: %-57s [ ${RED}FAIL${NC} ]\n" "$(basename "$SRC_MEDIA_FILE")"
        src_fail_count=$((src_fail_count + 1))
    else
        printf "Source Verify: %-57s [ ${GREEN}PASS${NC} ]\n" "$(basename "$SRC_MEDIA_FILE")"
    fi
done < <(find "$SOURCE_DIR" -type f -name "*.md5" ! -path '*/.*' -print0)

if [ "$src_fail_count" -gt 0 ] && [ "$IGNORE_SRC_FAIL" = false ]; then
    echo -e "\n${RED}CRITICAL FAIL: $src_fail_count source failures found. Aborting sync.${NC}"
    exit 1
fi

# 3. SYNC
echo -e "\n${YELLOW}[STEP 3] SYNCING DATA${NC}"
echo "-------------------------------------------------------------------------"
rsync -av --stats --exclude='.*' --include='*/' \
    --include='*.[mM][pP]4' --include='*.[mM][kK][vV]' --include='*.[aA][vV][iI]' \
    --include='*.[mM][pP][gG]' --include='*.[mM][pP][eE][gG]' --include='*.[mM][oO][vV]' \
    --include='*.[mM]4[vV]' --include='*.[fF][lL][vV]' --include='*.[wW][mM][vV]' \
    --include='*.[wW][aA][vV]' --include='*.[mM]4[aA]' --include='*.[mM][pP]3' \
    --include='*.[jJ][pP]2' --include='*.[tT][iI][fF]' --include='*.[tT][iI][fF][fF]' \
    --include='*.[jJ][pP][gG]' --include='*.[jJ][pP][eE][gG]' --include='*.[pP][nN][gG]' \
    --include='*.[tT][xX][tT]' --include='*.[dD][oO][cC][xX]' --include='*.[xX][lL][sS][xX]' \
    --include='*.[pP][dD][fF]' --include='*.[zZ][iI][pP]' --include='*.[jJ][sS][oO][nN]' \
    --include='*.[xX][mM][lL]' --include='*.[vV][tT][tT]' --include='*.framemd5' \
    --include='*.md5' --exclude='*' "$SOURCE_DIR/" "$DEST_DIR/"

# 4. DESTINATION VERIFICATION
echo -e "\n${YELLOW}[STEP 4] DESTINATION VERIFICATION${NC}"
echo "-------------------------------------------------------------------------"
pass_count=0
dst_fail_count=0
while IFS= read -r -d '' DEST_MD5; do
    MEDIA_FILE="${DEST_MD5%.md5}"
    [[ ! -f "$MEDIA_FILE" ]] && continue
    printf "Dest Verify: %-59s " "$(basename "$MEDIA_FILE")"
    expected_md5=$(grep -oE '[a-fA-F0-9]{32}' "$DEST_MD5" | head -n 1 | tr '[:upper:]' '[:lower:]')
    actual_md5=$(command -v md5sum >/dev/null 2>&1 && md5sum -- "$MEDIA_FILE" | grep -oE '[a-fA-F0-9]{32}' | head -n 1 | tr '[:upper:]' '[:lower:]' || md5 -q -- "$MEDIA_FILE" | tr '[:upper:]' '[:lower:]')
    if [ "$actual_md5" = "$expected_md5" ]; then
        echo -e "[ ${GREEN}PASS${NC} ]"; pass_count=$((pass_count + 1))
    else
        echo -e "[ ${RED}FAIL${NC} ]"; dst_fail_count=$((dst_fail_count + 1))
    fi
done < <(find "$DEST_DIR" -type f -name "*.md5" ! -path '*/.*' -print0)

# 5. COMPARISON
echo -e "\n${YELLOW}[STEP 5] FINAL COMPARISON${NC}"
echo "-------------------------------------------------------------------------"
SRC_TMP=$(mktemp)
DST_TMP=$(mktemp)

(cd "$SOURCE_DIR" && find . -type f \( -iname "*.mp4" -o -iname "*.mkv" -o -iname "*.avi" -o -iname "*.mpg" -o -iname "*.mpeg" -o -iname "*.mov" -o -iname "*.m4v" -o -iname "*.flv" -o -iname "*.wmv" -o -iname "*.wav" -o -iname "*.m4a" -o -iname "*.mp3" -o -iname "*.jp2" -o -iname "*.tif" -o -iname "*.tiff" -o -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.txt" -o -iname "*.docx" -o -iname "*.xlsx" -o -iname "*.pdf" -o -iname "*.zip" -o -iname "*.json" -o -iname "*.xml" -o -iname "*.vtt" -o -iname "*.framemd5" -o -iname "*.md5" \) ! -path '*/.*' | sort > "$SRC_TMP")
(cd "$DEST_DIR" && find . -type f \( -iname "*.mp4" -o -iname "*.mkv" -o -iname "*.avi" -o -iname "*.mpg" -o -iname "*.mpeg" -o -iname "*.mov" -o -iname "*.m4v" -o -iname "*.flv" -o -iname "*.wmv" -o -iname "*.wav" -o -iname "*.m4a" -o -iname "*.mp3" -o -iname "*.jp2" -o -iname "*.tif" -o -iname "*.tiff" -o -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.txt" -o -iname "*.docx" -o -iname "*.xlsx" -o -iname "*.pdf" -o -iname "*.zip" -o -iname "*.json" -o -iname "*.xml" -o -iname "*.vtt" -o -iname "*.framemd5" -o -iname "*.md5" \) ! -path '*/.*' | sort > "$DST_TMP")

src_count=$(wc -l < "$SRC_TMP" | xargs)
dst_count=$(wc -l < "$DST_TMP" | xargs)
MISSING_LIST=$(comm -23 "$SRC_TMP" "$DST_TMP")
missing_count=$( [[ -z "$MISSING_LIST" ]] && echo 0 || echo "$MISSING_LIST" | wc -l | xargs )
SRC_SIZE=$(du -sh "$SOURCE_DIR" | awk '{print $1}')
DST_SIZE=$(du -sh "$DEST_DIR" | awk '{print $1}')

rm "$SRC_TMP" "$DST_TMP"

echo "========================================================================="
echo "REPORT SUMMARY"
echo "========================================================================="
echo "Source Size:            $SRC_SIZE"
echo "Destination Size:       $DST_SIZE"
echo "-------------------------------------------------------------------------"
echo "Source File Count:      $src_count"
echo "Destination File Count: $dst_count"

if [ "$src_count" -eq "$dst_count" ]; then
    echo -e "Count Verification:     [ ${GREEN}MATCHED${NC} ]"
else
    echo -e "Count Verification:     [ ${RED}MISMATCHED (FAIL)${NC} ]"
fi

echo "-------------------------------------------------------------------------"
echo "Files Verified (MD5):   $pass_count"
echo "Checksum Fails:         $dst_fail_count"
echo "Missing Files (Sync):   $missing_count"

# --- FINAL SUCCESS BANNER ---
if [ "$missing_count" -eq 0 ] && [ "$dst_fail_count" -eq 0 ] && [ "$src_count" -eq "$dst_count" ]; then
    echo -e "\n${BOLD_GREEN}✔ ALL FILES COPIED AND VERIFIED SUCCESSFULLY${NC}"
fi

if [[ $missing_count -gt 0 ]]; then
    echo -e "\n${RED}MISSING FILES LIST:${NC}\n$MISSING_LIST"
fi

echo "========================================================================="
echo "PROCESS END: $(date)"
echo "========================================================================="
printf "${NC}"