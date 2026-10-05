local T = require("tests.client_mod.support")
local Calendar = require("fieldkit/rules/calendar")

T.case("observed moon timeline", function()
    local calendar = Calendar.new()
    T.each("transition", {
        { name = "join mid-phase", day = 100, phase = "half", waxing = true, min = 105, max = 107 },
        { name = "narrow window", day = 101, phase = "half", waxing = true, min = 106, max = 107 },
        { name = "phase boundary", day = 102, phase = "threequarter", waxing = true, min = 106, max = 106 },
        { name = "full tonight", day = 105, phase = "full", min = 106, max = 106 },
        { name = "forced full invalidates", day = 106, phase = "full" },
        { name = "clean transition recalibrates", day = 107, phase = "threequarter", min = 127, max = 127 },
        { name = "caves", day = 107, phase = "threequarter", cave = true },
        { name = "awake altar", day = 107, phase = "threequarter", altar = true },
        { name = "unknown cycle", day = 107, phase = "threequarter", cycle = "alien_moon" },
        { name = "native glassed cycle", day = 107, phase = "threequarter", cycle = "glassed_default", min = 125, max = 127 },
        { name = "rollback invalidates", day = 3, phase = "threequarter" },
    }, function(row)
        local full = calendar:fullmoon({ cycles = row.day, moonphase = row.phase,
            iswaxingmoon = row.waxing == true, isalterawake = row.altar }, row.cave, row.cycle)
        assert(full.quality == (row.min and "estimated" or "unknown"))
        if row.min then assert(full.min_day == row.min and full.max_day == row.max) end
    end)
end)

T.each("season availability", {
    { name = "skip zero winter", winter = 0, next = "spring", total = 55 },
    { name = "ordinary winter", winter = 15, next = "winter", total = 70 },
    { name = "endless autumn", winter = 0, remaining = 10000, total = 55 },
    { name = "invalid length", winter = 0, autumn = 0 / 0, invalid = true },
}, function(row)
    local season = Calendar.seasons({ cycles = 3, season = "autumn", autumnlength = row.autumn or 20,
        winterlength = row.winter, springlength = 20, summerlength = 15, remainingdaysinseason = row.remaining })
    if row.invalid then assert(season.lengths == nil)
    else assert(season.next_season == row.next and season.total == row.total) end
end)
T.done()
