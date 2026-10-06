return function(callback)
    TheSim = TheSim or {}
    TheSim.LuaPrintRemote = function(_, line)
        assert(line:sub(-1) == "\n", "SDK output must include its newline")
        callback(line:sub(1, -2))
    end
end
