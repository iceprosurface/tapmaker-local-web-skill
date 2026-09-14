-- 本地 Runner 在内存 manifest 中注入；不复制进生产资源。
local Bridge = {}
function Bridge.register(build, commands, dispatch)
    if not build or build.is_local ~= true then return nil end
    assert(type(commands) == "table" and type(dispatch) == "function")
    local json = cjson or require("cjson")
    local allowed = {}
    for name, enabled in pairs(commands) do
        if type(name) == "string" and enabled == true then allowed[name] = true end
    end
    local prefix = "saves/tapmaker-local-"
    local function write(name, value)
        local content = json.encode(value)
        local file = File(prefix .. name .. ".json", FILE_WRITE)
        assert(file and file:IsOpen(), "mailbox_write_failed")
        file:WriteString(content)
        file:Close()
    end
    fileSystem:CreateDir("saves")
    write("request", {})
    write("response", {})
    write("registration", allowed)
    local lastId
    return function()
        local file = File(prefix .. "request.json", FILE_READ)
        if not file or not file:IsOpen() then return end
        local size = file.size
        if size == 0 or size > 4096 then file:Close(); return end
        local raw = file:ReadString()
        file:Close()
        local decoded, request = pcall(json.decode, raw)
        if not decoded or type(request) ~= "table" or type(request.id) ~= "string"
            or #request.id > 128 or request.id == lastId then return end
        lastId = request.id
        local command = request.command
        local ok, result
        if type(command) ~= "table" or allowed[command.type] ~= true then
            ok, result = true, {ok = false, error = "unsupported_command"}
        else
            ok, result = pcall(dispatch, command)
        end
        if not ok then result = {ok = false, error = "dispatch_failed", message = tostring(result)} end
        if result == nil then result = json.null end
        local written = pcall(write, "response", {id = request.id, result = result})
        if not written then write("response", {id = request.id, result = {ok = false, error = "result_not_serializable"}}) end
    end
end
return Bridge
