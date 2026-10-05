local M = {}

M.slots = { cookpot = 4, portablecookpot = 4, archive_cookpot = 4, portablespicer = 2 }

local aliases = {
    cookedsmallmeat = "smallmeat_cooked",
    cookedmonstermeat = "monstermeat_cooked",
    cookedmeat = "meat_cooked",
}

function M.valid_recipe(cooker, recipe)
    local count = M.slots[cooker]
    if not count or type(recipe) ~= "table" then return false end
    local entries = 0
    for slot, prefab in pairs(recipe) do
        if type(slot) ~= "number" or slot % 1 ~= 0 or slot < 1 or slot > count
            or type(prefab) ~= "string" or #prefab == 0 or #prefab > 100 then
            return false
        end
        entries = entries + 1
    end
    return entries == count
end

function M.preview(data, cooker, recipe)
    local names, tags = {}, {}
    for _, prefab in ipairs(recipe) do
        local name = aliases[prefab] or prefab
        names[name] = (names[name] or 0) + 1
        local ingredient = data.ingredients[name]
        for tag, value in pairs(ingredient and ingredient.tags or {}) do
            if type(value) == "number" then tags[tag] = (tags[tag] or 0) + value end
        end
    end
    local candidates, priority, uncertain = {}, -math.huge, false
    local recipes = data.recipes[cooker]
    if type(recipes) ~= "table" then return candidates, true end
    for name, rule in pairs(recipes) do
        local rank = type(rule) == "table" and (rule.priority or 0)
        if type(name) ~= "string" or type(rule) ~= "table" or type(rule.test) ~= "function"
            or type(rank) ~= "number" or rank ~= rank or math.abs(rank) == math.huge then
            uncertain = true
        else
            local ok, matches = pcall(rule.test, cooker, names, tags)
            if not ok then
                uncertain = true
            elseif matches and rank >= priority then
                if rank > priority then candidates, priority = {}, rank end
                candidates[#candidates + 1] = name
            end
        end
    end
    table.sort(candidates)
    return candidates, uncertain
end

function M.compatible(data, source, destination, recipe)
    if source == destination then return true end
    if not (source == "cookpot" and destination == "portablecookpot"
        or source == "portablecookpot" and destination == "cookpot") then return false end
    if not M.valid_recipe(source, recipe) or not M.valid_recipe(destination, recipe) then return false end
    local expected, uncertain = M.preview(data, source, recipe)
    local actual, incomplete = M.preview(data, destination, recipe)
    if uncertain or incomplete or #expected == 0 or #expected ~= #actual then return false end
    for index, prefab in ipairs(expected) do if actual[index] ~= prefab then return false end end
    return true
end

return M
