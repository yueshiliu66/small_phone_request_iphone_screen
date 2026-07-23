from fastapi import FastAPI, File, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
import asyncio, uuid, base64, os
import httpx  # 新增：用于发送Bark推送
from openai import OpenAI

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

tasks: dict = {}
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# 从环境变量读取Bark配置
BARK_KEY = os.getenv("BARK_KEY", "di5BZQXhqC6GXmoL7HVzPf")          # 你的Bark Device Key
BARK_SERVER = os.getenv("BARK_SERVER", "https://api.day.app")  # Bark服务器（默认官方）
SHORTCUT_NAME = os.getenv("SHORTCUT_NAME", "识图模拟")  # 你的快捷指令名称（需URL编码）

class ScreenshotTask:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.event = asyncio.Event()
        self.analysis_result: str | None = None
        self.status = "PENDING_APPROVAL"


# ============================================================
# 📲 Bark 推送通知（点击通知自动触发快捷指令）
# ============================================================
async def push_bark_notification(task_id: str):
    """向手机推送Bark通知，点击后自动运行快捷指令并传入task_id"""
    if not BARK_KEY:
        return  # 未配置则跳过，退化为手动轮询模式

    # shortcuts://run-shortcut?name=快捷指令名&input=task_id
    import urllib.parse
    shortcut_url = (
        f"shortcuts://run-shortcut"
        f"?name={urllib.parse.quote(SHORTCUT_NAME)}"
        f"&input={task_id}"
    )

    payload = {
        "title": "📱 AI 读屏请求",
        "body": "点击此通知 → 自动截图并发送给 AI",
        "device_key": BARK_KEY,
        "url": shortcut_url,          # 点击通知时跳转的URL
        "sound": "minuet",            # 提示音（可改其他）
        "level": "active",            # 立即显示，不受专注模式屏蔽
        "autoCopy": "0",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client_http:
            await client_http.post(f"{BARK_SERVER}/push", json=payload)
    except Exception as e:
        print(f"[Bark推送失败] {e}")  # 不影响主流程


# ============================================================
# ① MCP Streamable HTTP 端点
# ============================================================
@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body = await request.json()
    method = body.get("method")
    req_id = body.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "screen-reader-mcp", "version": "1.0.0"}
            }
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "tools": [{
                    "name": "request_screen",
                    "description": (
                        "请求读取用户当前手机屏幕内容并进行 AI 图像分析，"
                        "会向用户手机推送确认通知，用户点击通知后自动截图并返回屏幕内容描述。"
                    ),
                    "inputSchema": {"type": "object", "properties": {}}
                }]
            }
        }

    if method == "tools/call":
        tool_name = body.get("params", {}).get("name")

        if tool_name == "request_screen":
            task_id = str(uuid.uuid4())
            task = ScreenshotTask(task_id)
            tasks[task_id] = task

            # ⬇️ 关键：创建任务后立即推送 Bark 通知
            await push_bark_notification(task_id)

            try:
                await asyncio.wait_for(task.event.wait(), timeout=55.0)
            except asyncio.TimeoutError:
                tasks.pop(task_id, None)
                return {
                    "jsonrpc": "2.0", "id": req_id,
                    "result": {
                        "content": [{"type": "text",
                                     "text": "⏰ 超时：55 秒内未收到截图，请检查手机通知或手动运行快捷指令。"}],
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
# ② iPhone 备用轮询（Bark未配置时的降级方案）
# ============================================================
@app.get("/iphone/check")
async def iphone_check():
    for task in tasks.values():
        if task.status == "PENDING_APPROVAL":
            return {"action": "approval_required", "task_id": task.task_id}
    return {"action": "idle"}


# ============================================================
# ③ 用户拒绝截图
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
# ④ 上传截图并 AI 分析
# ============================================================
from fastapi import FastAPI, File, UploadFile, Request
# 把 iphone_upload 这个函数整个替换掉

@app.post("/iphone/upload/{task_id}")
async def iphone_upload(task_id: str, request: Request):
    task = tasks.get(task_id)
    if not task:
        return {"status": "task_not_found"}

    # 兼容两种格式：JSON base64 或 multipart form
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        # iOS 快捷指令发来的 base64 JSON
        body = await request.json()
        base64_image = body.get("image", "")
        if not base64_image:
            return {"status": "error", "message": "image 字段为空"}
    else:
        # 尝试 multipart form（备用）
        try:
            form = await request.form()
            file = form.get("file")
            if not file:
                return {"status": "error", "message": "找不到 file 字段"}
            image_bytes = await file.read()
            base64_image = base64.b64encode(image_bytes).decode("utf-8")
            del image_bytes
        except Exception as e:
            return {"status": "error", "message": f"解析请求失败: {str(e)}"}

    # AI 分析
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

    del base64_image

    task.analysis_result = analysis
    task.status = "DONE"
    task.event.set()
    return {"status": "analysis_done", "preview": analysis[:50]}

