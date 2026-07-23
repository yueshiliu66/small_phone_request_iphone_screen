from fastapi import FastAPI, File, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
import asyncio, uuid, base64, os
from openai import OpenAI

app = FastAPI()

# 允许来自网页伴侣的跨域请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

tasks: dict = {}
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


class ScreenshotTask:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.event = asyncio.Event()
        self.analysis_result: str | None = None
        self.status = "PENDING_APPROVAL"


# ============================================================
# ① MCP Streamable HTTP 端点（plugin.js 对接的核心）
#    plugin 配置 URL 填：https://你的域名/mcp
# ============================================================
@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body = await request.json()
    method = body.get("method")
    req_id = body.get("id")

    # --- MCP 握手 ---
    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "screen-reader-mcp", "version": "1.0.0"}
            }
        }

    # --- 返回工具列表 ---
    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "tools": [{
                    "name": "request_screen",
                    "description": "请求读取用户当前手机屏幕内容并进行 AI 分析，返回屏幕上的核心信息描述。",
                    "inputSchema": {"type": "object", "properties": {}}
                }]
            }
        }

    # --- 工具调用（挂起等待 iPhone 上传截图） ---
    if method == "tools/call":
        tool_name = body.get("params", {}).get("name")

        if tool_name == "request_screen":
            task_id = str(uuid.uuid4())
            task = ScreenshotTask(task_id)
            tasks[task_id] = task

            try:
                # 最长等 55 秒（Render 免费版请求超时约 30s，建议升级或自行部署）
                await asyncio.wait_for(task.event.wait(), timeout=55.0)
            except asyncio.TimeoutError:
                tasks.pop(task_id, None)
                return {
                    "jsonrpc": "2.0", "id": req_id,
                    "result": {
                        "content": [{"type": "text",
                                     "text": "⏰ 超时：手机端 55 秒内未响应，请让用户手动运行截图快捷指令后重试。"}],
                        "isError": False
                    }
                }

            result = task.analysis_result
            tasks.pop(task_id, None)

            msg = "🚫 用户拒绝了截图请求。" if result == "USER_CANCELLED" else result
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": msg}],
                    "isError": False
                }
            }

        return {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"未知工具: {tool_name}"}
        }

    return {
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": -32601, "message": f"未知方法: {method}"}
    }


# ============================================================
# ② iPhone 快捷指令——检查是否有待处理截图请求
#    快捷指令第一步：GET /iphone/check
# ============================================================
@app.get("/iphone/check")
async def iphone_check():
    for task in tasks.values():
        if task.status == "PENDING_APPROVAL":
            return {"action": "approval_required", "task_id": task.task_id}
    return {"action": "idle"}


# ============================================================
# ③ iPhone 快捷指令——用户拒绝截图
#    快捷指令拒绝分支：POST /iphone/cancel/{task_id}
# ============================================================
@app.post("/iphone/cancel/{task_id}")
async def iphone_cancel(task_id: str):
    task = tasks.get(task_id)
    if not task:
        return {"status": "task_not_found"}
    task.analysis_result = "USER_CANCELLED"
    task.status = "CANCELLED"
    task.event.set()
    return {"status": "cancelled"}


# ============================================================
# ④ iPhone 快捷指令——确认并上传截图（AI 在这里分析图片）
#    快捷指令确认分支：POST /iphone/upload/{task_id}
# ============================================================
@app.post("/iphone/upload/{task_id}")
async def iphone_upload(task_id: str, file: UploadFile = File(...)):
    task = tasks.get(task_id)
    if not task:
        return {"status": "task_not_found"}

    image_bytes = await file.read()
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    del image_bytes  # 立刻释放

    try:
        response = client.chat.completions.create(
            model="[芋泥-anti-0.01]gemini-2.5-flash",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": "请详细描述这张手机截图的核心内容，包括显示的文字、图像信息、界面状态等。"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }],
            max_tokens=500
        )
        analysis = response.choices[0].message.content
    except Exception as e:
        analysis = f"⚠️ AI 分析失败：{str(e)}"

    del base64_image  # 分析完立刻删除

    task.analysis_result = analysis
    task.status = "DONE"
    task.event.set()  # 唤醒正在等待的 /mcp 请求
    return {"status": "analysis_done", "preview": analysis[:50]}
