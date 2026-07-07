#!/bin/bash
# progress.bin の配置・読み込み・効果をワーカー相当の形で自動判定する。
# ワーカーのインスタンス上 (~/shogibench-worker/Client がある環境) で実行する。
#
#   bash progress_check.sh                # 最新ビルドを自動選択
#   bash progress_check.sh バイナリ名      # Engines/ 内の名前を指定
#
# 判定の考え方:
#   1. 配置       : Networks/<sha>-dir/ に nn.bin と progress.bin が揃い sha が正しいか
#   2. オプション : usi 出力に progress 系オプションが存在するか、正確な名前は何か
#   3. 実パス     : 本番同等のオプションで isready してエラーが出ないか
#   4. 偽パス     : 存在しないパスなら明示的にエラーが出るか (出れば読込経路は健全)
#   5. 効果       : 固定ノード bench が progress あり/なしで変わるか (変われば読込確定)
cd ~/shogibench-worker/Client || exit 1
LOGDIR=/tmp/progress_check; mkdir -p "$LOGDIR"; V=()
ok(){ echo "  [OK] $*"; V+=("OK  $*"); }
ng(){ echo "  [NG] $*"; V+=("NG  $*"); }
qq(){ echo "  [??] $*"; V+=("??  $*"); }
sha8(){ sha256sum "$1" | cut -c1-8 | tr 'a-f' 'A-F'; }

BIN=${1:-$(ls -t Engines/ | head -1)}
[ -f "Engines/$BIN" ] || { echo "Engines/$BIN がありません:"; ls Engines/; exit 1; }
NET=$(echo "$BIN" | awk -F- '{print $(NF-1)}')
D=Networks/$NET-dir
echo "対象: Engines/$BIN (ネット $NET)"

echo "===== 1. 配置 ====="
if [ ! -d "$D" ]; then ng "$D が存在しない(このネットの対局/benchがまだ走っていない)"; exit 1; fi
ls -la "$D"
[ "$(sha8 "$D/nn.bin")" = "$NET" ] && ok "nn.bin のsha8がネット名と一致" || ng "nn.bin のsha8不一致!"
if [ -f "$D/progress.bin" ]; then
  ok "progress.bin あり (sha8=$(sha8 "$D/progress.bin") ←手元原本のsha256sumと比較)"
else
  ng "progress.bin が無い(登録時の名前が厳密に progress.bin だったか確認)"
fi

cd Engines; E=$(cd .. && pwd)/$D   # 対局時と同じカレント + 絶対パス

echo "===== 2. オプション存在(usi) ====="
(printf 'usi\n'; sleep 3; printf 'quit\n') | timeout 15 "./$BIN" > "$LOGDIR/usi.txt" 2>&1
grep -i progress "$LOGDIR/usi.txt"
# オプション「名」に progress を含むものを探す (LS_BUCKET_MODE の変種名には反応しない)
PROGOPT=$(awk 'tolower($1)=="option" && $2=="name" && tolower($3) ~ /progress/ {print $3}' "$LOGDIR/usi.txt" \
          | grep -iv 'slowmover\|mtg\|bucket' | head -1)
if [ -z "$PROGOPT" ]; then ng "progress系オプションがこのビルドに存在しない(エディション/ビルドフラグの問題)"
else ok "progressファイル用オプション: $PROGOPT (ShogiBenchの想定は LS_PROGRESS_COEFF)"; fi

if [ -n "$PROGOPT" ]; then
  echo "===== 3. 実パスで isready ====="
  (printf 'usi\nsetoption name EvalDir value %s\nsetoption name %s value %s/progress.bin\nisready\n' "$E" "$PROGOPT" "$E"; \
   sleep 8; printf 'quit\n') | timeout 40 "./$BIN" > "$LOGDIR/real.txt" 2>&1
  grep -Ei 'info string|error|warn|fail' "$LOGDIR/real.txt"

  echo "===== 4. 偽パスで isready(対照) ====="
  (printf 'usi\nsetoption name EvalDir value %s\nsetoption name %s value %s/no_such.bin\nisready\n' "$E" "$PROGOPT" "$E"; \
   sleep 8; printf 'quit\n') | timeout 40 "./$BIN" > "$LOGDIR/fake.txt" 2>&1
  grep -Ei 'info string|error|warn|fail' "$LOGDIR/fake.txt"
  if ! diff -q <(grep -Ei 'error|warn|fail' "$LOGDIR/real.txt") <(grep -Ei 'error|warn|fail' "$LOGDIR/fake.txt") >/dev/null; then
    ok "偽パスでのみエラー=実パスは実際に読まれている"
  else
    qq "実パスと偽パスで挙動が同じ=パスを読んでいない疑い(3,4のログ比較を)"
  fi

  echo "===== 5. 固定ノードbench比較 ====="
  bnch(){ { printf 'setoption name EvalDir value %s\n' "$E"; \
            [ -n "$1" ] && printf 'setoption name %s value %s\n' "$PROGOPT" "$1"; \
            printf 'bench 16 1 100000 default nodes\n'; sleep 20; printf 'quit\n'; } \
          | timeout 60 "./$BIN" 2>&1 | grep -i 'nodes searched' | grep -o '[0-9]*'; }
  N1=$(bnch "$E/progress.bin"); N2=$(bnch "")
  echo "  progressあり: ${N1:-失敗}  /  progressなし: ${N2:-失敗}"
  if [ -z "$N1" ] || [ -z "$N2" ]; then ng "benchが完走しない"
  elif [ "$N1" != "$N2" ]; then ok "bench値が変化=progressは評価/探索に効いている(読み込み成功)"
  else qq "bench値が不変=読めていないか、progressが探索に影響しない実装"; fi
fi

echo; echo "========== 判定まとめ =========="
printf '%s\n' "${V[@]}"
echo "ログ一式: $LOGDIR/ (usi.txt / real.txt / fake.txt)"
