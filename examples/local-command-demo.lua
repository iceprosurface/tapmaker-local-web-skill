-- 同一个 dispatcher 可供 UI、LuaJIT 与本地桥调用。
local state = {score = 0}
local function dispatch(command)
    if command.type == "query" then return {score = state.score} end
    if command.type == "add_score" then
        local amount = command.amount
        if type(amount) ~= "number" or amount % 1 ~= 0 or amount < 1 or amount > 100 then
            return {ok = false, error = "invalid_amount"}
        end
        state.score = state.score + amount
        return {ok = true, score = state.score}
    end
    return {ok = false, error = "unsupported_command"}
end
if ... == nil then
    assert(dispatch({type = "add_score", amount = 2}).score == 2)
    assert(dispatch({type = "add_score", amount = -1}).ok == false)
    assert(dispatch({type = "query"}).score == 2)
    print('{"ok":true,"score":2}')
end
return dispatch
