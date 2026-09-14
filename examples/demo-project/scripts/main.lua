local UI = require("urhox-libs/UI")

local uiRoot_ = nil
local bridgePoll_ = nil

function DemoLocalUpdate()
    if bridgePoll_ then bridgePoll_() end
end
local gameStarted_ = false
local score_ = 0

local statusLabel_ = nil
local scoreLabel_ = nil
local startButton_ = nil

local function updateScore()
    scoreLabel_:SetText("当前得分  " .. tostring(score_))
end

local function startGame()
    gameStarted_ = true
    score_ = 0
    statusLabel_:SetText("游戏已开始，点击加分按钮试试看！")
    startButton_:SetText("重新开始")
    updateScore()
    print("[tapmaker-local-web-demo] game started")
end

local function addScore()
    if not gameStarted_ then
        statusLabel_:SetText("请先点击“开始游戏”")
        return
    end

    score_ = score_ + 1
    statusLabel_:SetText("按钮响应正常，本地交互已生效。")
    updateScore()
end

local function resetGame()
    gameStarted_ = false
    score_ = 0
    statusLabel_:SetText("准备就绪，等待开始。")
    startButton_:SetText("开始游戏")
    updateScore()
end

local function createInterface()
    statusLabel_ = UI.Label {
        text = "准备就绪，等待开始。",
        width = "100%",
        fontSize = 16,
        textAlign = "center",
        color = UI.Theme.Color("textSecondary"),
    }

    scoreLabel_ = UI.Label {
        text = "当前得分  0",
        width = "100%",
        fontSize = 30,
        fontWeight = "bold",
        textAlign = "center",
        color = UI.Theme.Color("text"),
    }

    startButton_ = UI.Button {
        text = "开始游戏",
        width = "100%",
        height = 52,
        variant = "primary",
        onClick = startGame,
    }

    local actionRow = UI.Panel {
        width = "100%",
        flexDirection = "row",
        gap = 12,
        children = {
            UI.Button {
                text = "+1 加分",
                height = 46,
                flexGrow = 1,
                variant = "secondary",
                onClick = addScore,
            },
            UI.Button {
                text = "重置",
                height = 46,
                flexGrow = 1,
                variant = "outlined",
                onClick = resetGame,
            },
        },
    }

    return UI.Panel {
        width = "100%",
        height = "100%",
        padding = 24,
        justifyContent = "center",
        alignItems = "center",
        backgroundColor = UI.Theme.Color("background"),
        children = {
            UI.Panel {
                width = 430,
                maxWidth = "100%",
                padding = 28,
                gap = 18,
                flexDirection = "column",
                backgroundColor = UI.Theme.Color("surface"),
                borderColor = UI.Theme.Color("border"),
                borderWidth = 1,
                borderRadius = 18,
                children = {
                    UI.Label {
                        text = "TAPMAKER LOCAL WEB",
                        width = "100%",
                        fontSize = 13,
                        fontWeight = "bold",
                        textAlign = "center",
                        color = UI.Theme.Color("primary"),
                    },
                    UI.Label {
                        text = "本地预览 Demo",
                        width = "100%",
                        fontSize = 32,
                        fontWeight = "bold",
                        textAlign = "center",
                        color = UI.Theme.Color("text"),
                    },
                    UI.Label {
                        text = "不上传项目，也能验证 UI、按钮与热重载。",
                        width = "100%",
                        fontSize = 15,
                        textAlign = "center",
                        color = UI.Theme.Color("textSecondary"),
                    },
                    UI.Divider {},
                    scoreLabel_,
                    statusLabel_,
                    startButton_,
                    actionRow,
                    UI.Label {
                        text = "修改 main.lua 并保存，页面会自动重新加载。",
                        width = "100%",
                        fontSize = 12,
                        textAlign = "center",
                        color = UI.Theme.Color("textTertiary"),
                    },
                },
            },
        },
    }
end

function Start()
    graphics.windowTitle = "TapMaker Local Web Demo"
    UI.Init {
        fonts = {
            {
                family = "sans",
                weights = {
                    normal = "Fonts/MiSans-Regular.ttf",
                    bold = "Fonts/MiSans-Bold.ttf",
                },
            },
        },
        scale = UI.Scale.DEFAULT,
    }

    uiRoot_ = createInterface()
    UI.SetRoot(uiRoot_)
    -- UI 按钮与桥接命令复用上面的应用操作；生产不含生成模块。
    local isLocal, build = pcall(require, "__tapmaker_local_build_info")
    if isLocal and build.is_local == true then
        bridgePoll_ = require("__tapmaker_local_bridge").register(build,
            {query = true, start = true, add_score = true, reset = true},
            function(command)
                if command.type == "start" then startGame()
                elseif command.type == "add_score" then addScore()
                elseif command.type == "reset" then resetGame() end
                return {started = gameStarted_, score = score_}
            end)
        SubscribeToEvent("Update", "DemoLocalUpdate")
    end
    print("[tapmaker-local-web-demo] interface ready")
end

function Stop()
    bridgePoll_ = nil
    UnsubscribeFromEvent("Update")
    UI.Shutdown()
    uiRoot_ = nil
end

return true
