import json
import os
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from google import genai
from google.genai import types

# Подключаем логику партнера
from engineering.rag.index import KnowledgeBase
from engineering.rag.retriever import retrieve

# Инициализируем базу знаний при старте сервера
kb = KnowledgeBase.load()

app = FastAPI()
# ... дальше ваш код с CORSMiddleware ...

# Берем ключ из безопасного хранилища переменных окружения
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

# Allow the frontend to talk to this server (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. The Rulebook
system_instruction = """
You are an expert mechanical engineer and SolidWorks Python API developer.
Write a Python script using win32com.client to automate SolidWorks based on the user's parameters and image.
CRITICAL RULE: Output ONLY valid Python code. Do NOT wrap the code in markdown blocks (do not use ```python).
"""

# 3. The API Endpoint
@app.post("/generate")
async def generate_script(data: str = Form(...), image: UploadFile = File(None)):
    
    params = json.loads(data)
    user_text = params['prompt']
    
    # 1. Ищем инструкции в базе знаний
    rag_data = retrieve(kb=kb, message=user_text, domain="statics")
    rag_context = rag_data.as_prompt_block()

    # 2. Собираем промпт с учетом найденной информации
    user_prompt = f"""
    Create a SolidWorks Python script using these exact parameters.
    
    [KNOWLEDGE BASE CONTEXT]
    {rag_context}
    [/KNOWLEDGE BASE CONTEXT]

    - Problem Description: {params['prompt']}
    - Material: {params['material']['name']} (Yield={params['material']['yieldStrengthMPa']} MPa)
    - Geometry: Length={params['geometry']['lengthM']}m
    - Force: {params['loads']['forceN']} N
    """
    
    contents = [user_prompt]
    # ... дальше ваш старый код с добавлением картинки и вызовом Gemini ...

    # If the user uploaded an image, attach it!
    if image:
        image_bytes = await image.read()
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=image.content_type)
        contents.append(image_part)

    # Ask Gemini to generate the code
    try:
        response = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_instruction)
        )
        
        # Clean up the response just in case the AI added markdown backticks
        clean_code = response.text.replace("```python", "").replace("```", "").strip()
        
        # Send the raw code back to the frontend
        # Clean up the response just in case the AI added markdown backticks
        clean_code = response.text.replace("```python", "").replace("```", "").strip()
        
        # Собираем JSON по требуемой структуре
        response_data = {
            "code": clean_code,
            "parsed": {
                "lengthM": params['geometry']['lengthM'],
                "widthM": params['geometry']['widthM'],
                "heightM": params['geometry']['heightM'],
                "fixture": params['loads']['fixture'],
                "forceN": params['loads']['forceN'],
                "materialName": params['material']['name'],
                "yieldStrengthMPa": params['material']['yieldStrengthMPa']
            }
        }
        
        return JSONResponse(content=response_data)

    except Exception as e:
        error_msg = str(e)
        print(f"\n🚨 CRASH REPORT: {error_msg}\n")
        
        # Проверяем, это перегрузка Google (503) или другая ошибка
        if "503" in error_msg or "UNAVAILABLE" in error_msg:
            user_message = "# Сервер нейросети временно перегружен. Пожалуйста, подождите минуту и нажмите Generate снова."
            status = 503
        else:
            user_message = f"# Произошла ошибка при генерации: {error_msg}"
            status = 500
            
        error_data = {
            "code": user_message,
            "parsed": {} 
        }
        return JSONResponse(content=error_data, status_code=status)

if __name__ == "__main__":
    import uvicorn
    # Starts the server on port 8000
    uvicorn.run(app, host="127.0.0.1", port=8000)
    