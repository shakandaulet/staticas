import json
import os
import time
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from google import genai
from google.genai import types

# Import the partner's logic
from engineering.rag.index import KnowledgeBase
from engineering.rag.retriever import retrieve

# Initialize the knowledge base on serverstartup
kb = KnowledgeBase.load()

app = FastAPI()

# Get the key from the secure environment variables
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

# 2. THE RULEBOOK (Исправлено: жесткий приказ не копировать шаблоны)
system_instruction = """
You are an expert mechanical engineer and SolidWorks Python API developer.
CRITICAL RULES:
1. Output ONLY valid Python code using win32com.client. Do NOT wrap the code in markdown blocks (do not use ```python).
2. You MUST use the exact numerical parameters (geometry, forces, material properties) provided by the user.
3. Do NOT blindly copy templates from the knowledge base. Use the knowledge base for syntax and logic, but insert the user's specific numbers and conditions.
"""

# 3. The API Endpoint
@app.post("/generate")
async def generate_script(data: str = Form(...), image: UploadFile = File(None)):
    
    params = json.loads(data)
    user_text = params.get('prompt', '').strip()
    
    # 1. Search for instructions in the knowledge base
    if user_text:
        try:
            rag_data = retrieve(kb=kb, message=user_text, domain="statics")
            rag_context = rag_data.as_prompt_block()
        except Exception as e:
            print(f"⚠️ RAG Error. Error: {e}")
            rag_context = "No additional context available."
    else:
        rag_context = "No text description provided. Rely strictly on the attached image."
        user_text = "Please analyze the attached image."

    # 2. Промпт (Исправлено: добавлены ВСЕ переменные из вашей формы)
    user_prompt = f"""
    Create a SolidWorks Python script. You must strictly apply the parameters listed in the [USER TASK] block below.
    
    [KNOWLEDGE BASE CONTEXT]
    {rag_context}
    [/KNOWLEDGE BASE CONTEXT]

    [USER TASK PARAMETERS]
    - Problem Description: {user_text}
    - Material Name: {params['material']['name']}
    - Material Properties: Yield = {params['material']['yieldStrengthMPa']} MPa, Young's Modulus = {params['material']['youngsModulusGPa']} GPa, Poisson = {params['material']['poissonsRatio']}
    - Geometry: Length = {params['geometry']['lengthM']} m, Width = {params['geometry']['widthM']} m, Height = {params['geometry']['heightM']} m
    - Loads: Fixture Type = {params['loads']['fixture']}, Force = {params['loads']['forceN']} N
    - Mesh Quality: {params['mesh']}
    [/USER TASK PARAMETERS]
    """
    
    contents = [user_prompt]

    if image:
        image_bytes = await image.read()
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=image.content_type)
        contents.append(image_part)

    # 3. Request to Gemini (Исправлено: добавлена temperature=0.1 для точности)
    max_retries = 3
    response = None
    error_msg = ""
    
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-1.5-flash',
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.1  # Делает нейросеть точной и менее "креативной"
                )
            )
            break
            
        except Exception as e:
            error_msg = str(e)
            print(f"⚠️ Attempt {attempt + 1}/{max_retries} failed: {error_msg}")
            
            if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                time.sleep(65)
            elif "503" in error_msg or "UNAVAILABLE" in error_msg:
                time.sleep(15)
            elif "400" in error_msg:
                break
            else:
                time.sleep(5)

    if not response:
        status = 503 if "503" in error_msg or "429" in error_msg else 500
        user_message = "# API limits exhausted. Please wait a moment and try again."
        return JSONResponse(status_code=status, content={"code": user_message, "parsed": {}})

    # 4. Process the successful response
    clean_code = response.text.replace("```python", "").replace("```", "").strip()
    
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

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
