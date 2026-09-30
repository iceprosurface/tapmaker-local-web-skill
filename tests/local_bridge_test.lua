-- 用内存 File 和 codec 替身测试 Lua 协议，不启动引擎。
local files, values, counter = {}, {}, 0
cjson = {null = {}}
function cjson.encode(value)
    if type(value) == 'table' and type(value.result) == 'function' then error('bad result') end
    counter = counter + 1
    local key = tostring(counter)
    values[key] = value
    return key
end
function cjson.decode(raw) assert(values[raw], 'bad json'); return values[raw] end
FILE_WRITE, FILE_READ = 1, 2
fileSystem = {CreateDir = function() end}
function File(name, mode)
    return {size = #(files[name] or ''), IsOpen = function() return true end,
        WriteString = function(_, raw) files[name] = raw end,
        ReadString = function() return files[name] end, Close = function() end}
end
local bridge = dofile(arg[1])
local executions = 0
local function dispatch(command)
    executions = executions + 1
    if command.fail then error('failure') end
    if command.bad then return function() end end
    return {score = executions}
end
assert(bridge.register({is_local=false}, {query=true}, dispatch) == nil)
assert(next(files) == nil)
local poll = bridge.register({is_local=true}, {query=true}, dispatch)
local function request(id, command)
    files['saves/tapmaker-local-request.json'] = cjson.encode({id=id, command=command})
    poll()
    return cjson.decode(files['saves/tapmaker-local-response.json']).result
end
assert(request('1', {type='query'}).score == 1)
poll(); assert(executions == 1)
assert(request('2', {type='eval'}).error == 'unsupported_command')
assert(executions == 1)
assert(request('3', {type='query', fail=true}).error == 'dispatch_failed')
assert(request('4', {type='query', bad=true}).error == 'result_not_serializable')
print('local bridge contracts passed')
