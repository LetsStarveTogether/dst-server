#!/usr/bin/env bash

INSTALL_PATH="/install"
CLUSTER_PATH="/cluster"
MODS_PATH="$CLUSTER_PATH/mods"
UGC_PATH="$MODS_PATH/ugc"

function fail() {
    echo Error: "$@" >&2
    exit 1
}

function check_for_file() {
    if [[ ! -f "$1" ]]; then
        fail "Missing file: $1"
    fi
}

# 信号处理，优雅关闭服务器
trap handle_term TERM
function handle_term() {
    killall -ws TERM 'dontstarve_dedicated_server_nullrenderer_x64'
    exit 0
}

# 软链接修改 mods 文件夹至存档下
if [[ ! -L "$INSTALL_PATH/mods" ]]; then
    rm -rf "$INSTALL_PATH/mods"
    ln -s "$MODS_PATH" "$INSTALL_PATH/mods"
fi

# 进入服务端目录
cd "$INSTALL_PATH/bin64" || fail "can't cd to $INSTALL_PATH/bin64"

# 检测分片文件夹名
mapfile -t shards < <(find "$CLUSTER_PATH" -mindepth 1 -maxdepth 1 -type d -not -name "mods" -exec basename {} \;)

# 检查分片必需配置文件
mod_ids=()
for shard in "${shards[@]}"; do
    shard_path="$CLUSTER_PATH/$shard"
    check_for_file "$shard_path/server.ini"
    mapfile -t -O "${#mod_ids[@]}" mod_ids < <(grep -Eo "workshop-[[:digit:]]+" "$shard_path/modoverrides.lua" | sed "s/workshop-//")
done

# 更新 mod
# 使用临时分片文件夹，无需配置文件
# 避免服务端启动默认创建 Master 分片文件夹
sorted_ids=()
mkdir -p $UGC_PATH
touch $MODS_PATH/modsettings.lua $MODS_PATH/dedicated_server_mods_setup.lua
truncate -s0 $MODS_PATH/dedicated_server_mods_setup.lua
mapfile -t sorted_ids < <(echo "${mod_ids[@]}" | tr ' ' '\n' | sort -nu)
for id in "${sorted_ids[@]}"; do
    echo "ServerModSetup(\"$id\")" >>$MODS_PATH/dedicated_server_mods_setup.lua
done

if [[ -f "$MODS_PATH/proxy" ]]; then
    export HTTP_PROXY="socks5://127.0.0.1:1080"
    export HTTPS_PROXY="socks5://127.0.0.1:1080"
fi

mkdir -p '/tmp/conf/c/s'
port=$((30000 + RANDOM))
./dontstarve_dedicated_server_nullrenderer_x64 \
    -only_update_server_mods \
    -monitor_parent_process $$ \
    -port "$port" \
    -steam_master_server_port "$((port + 1))" \
    -steam_authentication_port "$((port + 2))" \
    -ugc_directory "$UGC_PATH" \
    -persistent_storage_root '/tmp' \
    -conf_dir 'conf' \
    -cluster 'c' \
    -shard 's' | sed -u 's/^/[MOD_UPDATE]: /'
rm -rf '/tmp/conf'

unset HTTP_PROXY HTTPS_PROXY

# 检查集群必需配置文件
check_for_file "$CLUSTER_PATH/cluster.ini"
check_for_file "$CLUSTER_PATH/cluster_token.txt"

# 确保专服特有文件夹及文件存在
touch "$CLUSTER_PATH/adminlist.txt"
touch "$CLUSTER_PATH/whitelist.txt"
touch "$CLUSTER_PATH/blocklist.txt"

# 启动集群
for shard in "${shards[@]}"; do
    shard_path="$CLUSTER_PATH/$shard"
    check_for_file "$shard_path/server.ini"

    # 主分片的 console 作为集群 console
    if grep -qiE 'is_master[[:space:]]*=[[:space:]]*true' "$shard_path/server.ini"; then
        console_path="$CLUSTER_PATH/console"
    else
        console_path="$shard_path/console"
    fi

    # 创建命名管道
    if [[ ! -p "$console_path" ]]; then
        rm -f "$console_path"
        mkfifo "$console_path"
    fi

    # 启动分片
    # 分片配置路径应为 <persistent_storage_root>/<conf_dir>/<cluster>/<shard>/server.ini
    # 这里的配置最终形成的路径实际为 ////clutser/shard/server.ini
    # 这在 linux 环境下等价于 /cluster/shard/server.ini
    ./dontstarve_dedicated_server_nullrenderer_x64 \
        -skip_update_server_mods \
        -monitor_parent_process $$ \
        -persistent_storage_root / \
        -conf_dir / \
        -ugc_directory "$UGC_PATH" \
        -cluster "$(basename $CLUSTER_PATH)" \
        -shard "$shard" <>"$console_path" | sed -u "s/^/$shard: /" &
    # cloudserver 模式下 TERM 后不会自动终止进程，暂时不用此模式
    # -cloudserver 3<>"$console_path" 4>>"$shard_path/fd4" 5>>"$shard_path/fd5"
done
wait
