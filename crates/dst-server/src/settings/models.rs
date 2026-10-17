// Fixed configuration fields and INI sections.

ini_model! { ClusterSettings {
    "MISC" {
        #[garde(range(min = 1, max = 2_147_483_647))]
        max_snapshots: u64 = 6,
        #[garde(skip)]
        console_enabled: bool = true,
        #[garde(skip)]
        use_alternate_gc: bool = false,
        #[garde(skip)]
        mods_enabled: bool = true,
    }
    "SHARD" {
        #[garde(skip)]
        shard_enabled: bool = false,
        #[garde(ipv4, pattern("^[^\\x00\\r\\n]*$"))]
        bind_ip: String = "127.0.0.1".into(),
        #[garde(length(chars, min = 1), pattern("^[^\\x00\\r\\n]*$"))]
        master_ip: Option<String> = None,
        #[garde(range(min = 1_024, max = 65_535))]
        master_port: u64 = 10_888,
        #[garde(pattern("^[^\\x00\\r\\n]*$"))]
        cluster_key: Option<String> = None,
    }
    "STEAM" {
        #[garde(skip)]
        steam_group_only: bool = false,
        #[garde(skip)]
        steam_group_id: u64 = 0,
        #[garde(skip)]
        steam_group_admins: bool = false,
    }
    "NETWORK" {
        #[garde(length(chars, max = 80), pattern("^[^\\x00\\r\\n]*$"))]
        cluster_name: Option<String> = None,
        #[garde(length(chars, max = 254), pattern("^[^\\x00\\r\\n]*$"))]
        cluster_password: Option<String> = None,
        #[garde(length(chars, max = 254), pattern("^[^\\x00\\r\\n]*$"))]
        cluster_description: String = "".into(),
        #[garde(range(min = 1, max = 60))]
        tick_rate: u64 = 15,
        #[garde(skip)]
        offline_cluster: bool = false,
        #[garde(skip)]
        lan_only_cluster: bool = false,
        #[garde(skip)]
        autosaver_enabled: bool = true,
        #[garde(skip)]
        whitelist_slots: u64 = 0,
        #[garde(skip)]
        cluster_language: ClusterLanguage = ClusterLanguage::En,
        #[garde(range(max = 2_147_483_647))]
        connection_timeout: u64 = 8_000,
        #[garde(skip)]
        internet_broadcasting_enabled: bool = true,
        #[garde(range(max = 2_147_483_647))]
        idle_timeout: u64 = 1_800,
        #[garde(pattern("^[^\\x00\\r\\n]*$"))]
        override_dns: Option<String> = None,
    }
    "GAMEPLAY" {
        #[garde(range(min = 1, max = 64))]
        max_players: u64 = 16,
        #[garde(skip)]
        pvp: bool = false,
        #[garde(length(chars, min = 1), pattern("^[^\\x00\\r\\n]*$"))]
        game_mode: String = "survival".into(),
        #[garde(skip)]
        pause_when_empty: bool = false,
        #[garde(skip)]
        vote_enabled: bool = true,
    }
} }

ini_model! { ShardSettings {
    "SHARD" {
        #[garde(skip)]
        is_master: bool = true,
        #[garde(length(chars, min = 1), pattern("^[^\\x00\\r\\n]*$"))]
        name: Option<String> = None,
        #[garde(range(min = 1, max = 4_294_967_295))]
        id: Option<u64> = None,
        #[garde(ipv4, pattern("^[^\\x00\\r\\n]*$"))]
        bind_ip: Option<String> = None,
        #[garde(length(chars, min = 1), pattern("^[^\\x00\\r\\n]*$"))]
        master_ip: Option<String> = None,
        #[garde(range(min = 1_024, max = 65_535))]
        master_port: Option<u64> = None,
        #[garde(pattern("^[^\\x00\\r\\n]*$"))]
        cluster_key: Option<String> = None,
    }
    "STEAM" {
        #[garde(range(min = 1_024, max = 65_535))]
        master_server_port: u64 = 27_016,
    }
    "NETWORK" {
        #[garde(range(min = 1_024, max = 65_535))]
        server_port: u64 = 10_999,
    }
    "ACCOUNT" {
        #[garde(skip)]
        encode_user_path: bool = false,
    }
} }

sparse_model! {
    ClusterConfig {
        required {
            #[garde(dive, length(min = 1))]
            shards: BTreeMap<String, ShardConfig>,
        }
        optional {
            #[garde(pattern("^[^\\x00\\r]*$"))]
            adminlist: String = "".into(),
            #[garde(pattern("^[^\\x00\\r]*$"))]
            blocklist: String = "".into(),
            #[garde(dive)]
            downloads: WorkshopDownloads = WorkshopDownloads::default(),
            #[garde(dive)]
            mod_settings: ModSettings = ModSettings::default(),
            #[garde(dive)]
            settings: ClusterSettings = ClusterSettings::default(),
            #[garde(pattern("^[!-~]*$"))]
            token: String = "".into(),
            #[garde(pattern("^[^\\x00\\r]*$"))]
            whitelist: String = "".into(),
        }
    }
}

sparse_model! {
    ShardConfig {
        required {
            #[garde(dive)]
            settings: ShardSettings,
        }
        optional {
            #[garde(dive)]
            level: Option<LevelDataOverride> = None,
            #[garde(dive)]
            mods: ModOverrides = ModOverrides::default(),
            #[garde(dive)]
            world: Option<WorldgenOverride> = None,
        }
    }
}

sparse_model! {
    #[derive(Default)]
    RoomPreset {
        required {
        }
        optional {
            #[garde(dive)]
            mods: ModOverrides = ModOverrides::default(),
            #[garde(dive)]
            settings: ClusterSettings = ClusterSettings::default(),
            #[garde(dive)]
            shards: BTreeMap<String, ShardConfig> = BTreeMap::new(),
        }
    }
}

sparse_model! {
    #[derive(Default)]
    ModOverride {
        required {
        }
        optional {
            #[garde(dive)]
            configuration_options: Option<BTreeMap<String, LuaValue>> = None,
            #[garde(skip)]
            enabled: Option<bool> = None,
        }
    }
}

sparse_model! {
    #[derive(Default)]
    ModOverrides {
        required {
        }
        optional {
            #[garde(skip)]
            client_mods_disabled: Option<bool> = None,
            #[garde(dive)]
            entries: BTreeMap<String, ModOverride> = BTreeMap::new(),
        }
    }
}

sparse_model! {
    #[derive(Default)]
    ModSettings {
        required {
        }
        optional {
            #[garde(skip)]
            debug_print: bool = false,
            #[garde(skip)]
            disable_local_mod_warning: bool = false,
            #[garde(skip)]
            disable_mod_disabling: bool = false,
            #[garde(inner(inner(length(chars, min = 1))), custom(unique))]
            force_enabled: Vec<String> = Vec::new(),
            #[garde(skip)]
            mod_errors: bool = false,
        }
    }
}

sparse_model! {
    #[derive(Default)]
    WorkshopDownloads {
        required {
        }
        optional {
            #[garde(inner(inner(range(min = 1))), custom(unique))]
            collections: Vec<u64> = Vec::new(),
            #[garde(inner(inner(range(min = 1))), custom(unique))]
            items: Vec<u64> = Vec::new(),
        }
    }
}

sparse_model! {
    #[derive(Default)]
    WorldgenOverride {
        required {
        }
        optional {
            #[garde(skip)]
            enabled: bool = true,
            #[garde(dive)]
            overrides: WorldValues = WorldValues::default(),
            #[garde(length(chars, min = 1))]
            settings_preset: Option<String> = None,
            #[garde(length(chars, min = 1))]
            worldgen_preset: Option<String> = None,
        }
    }
}

sparse_model! {
    LevelDataOverride {
        required {
            #[garde(length(chars, min = 1))]
            id: String,
            #[garde(skip)]
            name: String,
            #[garde(skip)]
            desc: String,
            #[garde(length(chars, min = 1))]
            location: String,
            #[garde(dive)]
            overrides: WorldValues,
        }
        optional {
            #[garde(inner(inner(inner(range(min = -9_007_199_254_740_991, max = 9_007_199_254_740_991)))))]
            background_node_range: Option<[i64; 2]> = None,
            #[garde(length(chars, min = 1))]
            baseid: Option<String> = None,
            #[garde(length(chars, min = 1))]
            blocker_blank_room_name: Option<String> = None,
            #[garde(skip)]
            custom_settings_desc: Option<String> = None,
            #[garde(length(chars, min = 1))]
            custom_settings_id: Option<String> = None,
            #[garde(skip)]
            custom_settings_name: Option<String> = None,
            #[garde(skip)]
            custom_worldgen_desc: Option<String> = None,
            #[garde(length(chars, min = 1))]
            custom_worldgen_id: Option<String> = None,
            #[garde(skip)]
            custom_worldgen_name: Option<String> = None,
            #[garde(skip)]
            hideinfrontend: Option<bool> = None,
            #[garde(skip)]
            hideminimap: Option<bool> = None,
            #[garde(range(min = -9_007_199_254_740_991, max = 9_007_199_254_740_991))]
            max_playlist_position: Option<i64> = None,
            #[garde(range(min = -9_007_199_254_740_991, max = 9_007_199_254_740_991))]
            min_playlist_position: Option<i64> = None,
            #[garde(range(max = 9_007_199_254_740_991))]
            numrandom_set_pieces: Option<u64> = None,
            #[garde(inner(inner(inner(length(chars, min = 1)))))]
            ordered_story_setpieces: Option<Vec<String>> = None,
            #[garde(skip)]
            override_level_string: Option<bool> = None,
            #[garde(dive)]
            override_triggers: Option<BTreeMap<String, LuaValue>> = None,
            #[garde(length(chars, min = 1))]
            playstyle: Option<String> = None,
            #[garde(inner(inner(inner(length(chars, min = 1)))))]
            random_set_pieces: Option<Vec<String>> = None,
            #[garde(inner(inner(inner(length(chars, min = 1)))))]
            required_prefabs: Option<Vec<String>> = None,
            #[garde(inner(inner(inner(length(chars, min = 1)))))]
            required_setpieces: Option<Vec<String>> = None,
            #[garde(length(chars, min = 1))]
            settings_baseid: Option<String> = None,
            #[garde(skip)]
            settings_desc: Option<String> = None,
            #[garde(length(chars, min = 1))]
            settings_id: Option<String> = None,
            #[garde(skip)]
            settings_name: Option<String> = None,
            #[garde(dive)]
            substitutes: Option<BTreeMap<String, LuaValue>> = None,
            #[garde(range(min = 1, max = 9_007_199_254_740_991))]
            version: Option<u64> = None,
            #[garde(length(chars, min = 1))]
            worldgen_baseid: Option<String> = None,
            #[garde(skip)]
            worldgen_desc: Option<String> = None,
            #[garde(length(chars, min = 1))]
            worldgen_id: Option<String> = None,
            #[garde(skip)]
            worldgen_name: Option<String> = None,
        }
    }
}
