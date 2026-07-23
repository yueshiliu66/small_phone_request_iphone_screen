from fastapi import FastAPI, File, UploadFile
import asyncio
import uuid
import base64
import os
from openai import OpenAI

app = FastAPI()
tasks = {}
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

class ScreenshotTask:
    def __init__(self, task_id):
        self.task_id = task_id
        self.event = asyncio.Event()
        self.image_bytes = None
        self.analysis_result = None
        self.status = "PENDING_APPROVAL"  # 初始状态：等待用户批准

# ---------- 1. 伴侣调用的接口（挂起等待，最长 55 秒） ----------
@app.get("/mcp/request_screen")
async def request_screen():
    task_id = str(uuid.uuid4())
    task = ScreenshotTask(task_id)
    tasks[task_id] = task
    
    try:
        # 等待 iPhone 那边点击“确认”并传回图片，或用户点击“拒绝”
        await asyncio.wait_for(task.event.wait(), timeout=55.0)
    except asyncio.TimeoutError:
        tasks.pop(task_id, None)
        return {"status": "timeout", "message": "手机端 55 秒内未确认，请重试"}
    
    # 被唤醒了，检查是用户拒绝还是成功
    result = task.analysis_result
    tasks.pop(task_id, None)
    
    if result == "USER_CANCELLED":
        return {"status": "cancelled", "message": "用户在手机上拒绝了截图请求"}
    else:
        return {"status": "success", "analysis": result}

# ---------- 2. iPhone 轮询接口（看看有没有活要干） ----------
@app.get("/iphone/poll/{task_id}")
async def iphone_poll(task_id: str):
    task = tasks.get(task_id)
    if not task:
        return {"action": "idle"}
    
    if task.status == "PENDING_APPROVAL":
        # 告诉手机：有截图任务，需要用户确认
        return {"action": "approval_required", "task_id": task_id}
    
    return {"action": "idle"}

# ---------- 3. 用户拒绝截图（快捷指令调用） ----------
@app.post("/iphone/cancel/{task_id}")
async def iphone_cancel(task_id: str):
    task = tasks.get(task_id)
    if not task:
        return {"status": "task_not_found"}
    
    task.analysis_result = "USER_CANCELLED"
    task.status = "CANCELLED"
    task.event.set()  # 唤醒伴侣那边的请求
    return {"status": "cancelled"}

# ---------- 4. 用户确认并上传截图（快捷指令调用） ----------
@app.post("/iphone/upload/{task_id}")
async def iphone_upload(task_id: str, file: UploadFile = File(...)):
    task = tasks.get(task_id)
    if not task:
        return {"status": "task_not_found"}
    
    # 读取图片
    image_bytes = await file.read()
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    del image_bytes  # 立刻释放内存
    
    # 调用 AI 分析
    try:
        response = client.chat.completions.create(
            model="[芋泥-anti-0.01]gemini-2.5-flash",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请用简短文字描述这张手机截图里的核心内容。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            max_tokens=300
        )
        analysis = response.choices[0].message.content
    except Exception as e:
        analysis = f"AI 分析失败: {str(e)}"
    
    del base64_image  # 分析完立刻删除
    
    task.analysis_result = analysis
    task.status = "DONE"
    task.event.set()  # 唤醒伴侣
    return {"status": "analysis_done"}