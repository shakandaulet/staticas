# Statica Project Architectural Documentation (arc42 Methodology)



## 1. Introduction and Goals



Problem Statement and Significance: Engineering students and professionals allocate substantial time to manually constructing models and configuring calculations in SolidWorks. Often, the available input is limited to textual descriptions or textbook images. The Statica project addresses this inefficiency by serving as an interface between natural language processing and the complex SolidWorks Python API.



Primary Goal: To develop an AI-driven web application capable of converting textual descriptions and images of statics problems into executable Python scripts for SolidWorks 2024.



## 2. Architecture Constraints



- Infrastructure Constraints: Utilization of the Render free tier for the backend, which imposes limitations on port binding and includes server sleep periods during inactivity.

- API Constraints: Strict quotas applied by the Google Gemini API free tier (maximum of 20 requests per day per project, stringent RPM/TPM limits, and frequent 503 High Demand errors).

- SolidWorks Technical Constraints: Inability to execute SolidWorks scripts directly in cloud environments. Scripts must be executed locally on a user's Windows machine via the win32com library.



## 3. Context and Scope



The Statica system interacts with the following external actors and systems:



- User (Engineer/Student): Inputs parameters, uploads images, and receives the generated .py file.

- Google Gemini API (LLM): The core engine for natural language processing (NLP) and prompt-based code generation.

- Local SolidWorks Environment (External System): The target execution environment where the user will run the generated macro for simulation (Static Analysis).



## 4. Solution Strategy



- Frontend: A static web application utilizing HTML, CSS, and JavaScript, deployed on Netlify. Features real-time rendering of dynamic SVG schematics based on AI responses.

- Backend: A RESTful API built with Python and FastAPI, deployed on Render.

- Code Generation: Implementation of Retrieval-Augmented Generation (RAG). The backend retrieves syntax examples from a local vector knowledge base and supplies them to Gemini alongside form data.

- Conflict Resolution (Text vs. Form): Gemini functions as an intelligent filter. If user-provided text conflicts with form data, the AI prioritizes the text and enforces a JSON response containing the corrected parameters.



## 5. Building Block View



The system is divided into two primary levels:



Level 1: Frontend (Client-side):

The frontend consists of three pages served as six static files:

- index.html: Self-contained landing page (styles and animation inline). Presents the product and leads to the application via a single CTA. Features a CSS-3D rotating beam with an animated force arrow, built without external libraries.
 <img width="1913" height="982" alt="landing png" src="https://github.com/user-attachments/assets/76e7199b-9afd-4163-947f-d7806b0888b3" />

 
- app.html / app-script.js / app-style.css: The main application. Contains the Firebase authentication overlay (email/password sign-up and login, which blocks access to the form until authenticated), the problem input form (text prompt, image upload via click or drag-and-drop, material presets with auto-fill, geometry, loads, mesh density), and client-side validation of all 11 input fields before any network request is made. A single constant (BACKEND_URL) switches the page between demo mode (sample script, no server required) and live mode.
<img width="1905" height="986" alt="form png" src="https://github.com/user-attachments/assets/734a540f-c9a4-4352-ac6a-77cbf966349e" />


- result.html / result-script.js: The output page. Receives the generated code and task parameters from app.html via sessionStorage, renders an SVG free-body schematic (beam, support symbols per fixture type, force vector, dimension line), runs an independent hand-calculated stress check, and provides Copy and Download (.py) actions. Direct visits without generated data show a guided empty state instead of a broken page.



Level 2: Backend (FastAPI Server):

- server.py: The single entry point utilizing the /generate endpoint.

- KnowledgeBase (RAG Module): Local storage of embeddings providing win32com API syntax context.



## 6. Runtime View



Scenario: User requests script generation



1. The user signs up or logs in (Firebase Authentication); the form is inaccessible until authenticated.
   
2. The user inputs form data, adds a textual problem description, and initiates the "Generate" action. The frontend first validates all inputs locally (required problem statement or image, positive numeric values, Poisson's ratio within 0-0.5, image type/size limits); invalid fields are highlighted and no request is sent until the form is valid.
<img width="1145" height="943" alt="validation png" src="https://github.com/user-attachments/assets/2768586b-a945-4170-a7eb-21e000695c52" />

3. The frontend transmits a multipart/form-data payload to the /generate endpoint.

4. The backend extracts the text, queries the local RAG database, and constructs the prompt.

5. The backend calls the Gemini API with the response_mime_type="application/json" parameter enforced.

6. In the event of server overload (503) or quota exhaustion (429), the backend initiates an asynchronous retry loop.

7. Gemini returns a strictly formatted JSON object containing the script code and the recognized parameters.

8. The backend relays the data to the client, and the frontend updates the schematic representation accordingly (e.g., replacing a horizontal beam with a vertical one if axial tension is detected).

9. The result page displays the schematic, the stress estimate, and the generated script with Copy and Download (.py) actions. If the backend is unreachable, the frontend distinguishes between network failure, model overload (503), and internal server errors, and shows a specific message for each case.
<img width="1897" height="871" alt="result-code png" src="https://github.com/user-attachments/assets/26fb619c-82fb-484f-93f1-effd3865831f" />




## 7. Deployment View


- Frontend (production): statica-app.netlify.app — three pages served as six static files (pure HTML/CSS/JS, no build step or framework required), deployed manually to Netlify with drag-and-drop deploys, version history, and one-   click rollback. A mirror copy is auto-served from this repository via GitHub Pages.

- Authentication service: Firebase Authentication (Google-hosted). User credentials are stored and verified entirely on Google's infrastructure; the frontend only calls the Firebase SDK. Every domain that serves the frontend must   be whitelisted in Firebase Console -> Authentication -> Authorized domains, otherwise login fails on that domain.

- Backend: Deployed as a Web Service on Render (Docker/Python Environment). Execution is managed via uvicorn with dynamic port binding:



uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))



## 8. Crosscutting Concepts


### 8.1. Quota and Network Error Management

Issue: The Google API frequently returned 503 (Unavailable) and 429 (Resource Exhausted) errors, causing backend failures.

Resolution: Implementation of the asyncio module to create an exponential backoff loop that does not block other server processes.


```python
for attempt in range(max_retries):

    try:

        response = client.models.generate_content(...)

        break

    except Exception as e:

        if "429" in error_msg:

            await asyncio.sleep(65)

        elif "503" in error_msg:

            await asyncio.sleep(15)
```


### 8.2. Enforced Structured Output (JSON Enforcing)

Issue: The LLM demonstrated a tendency to hallucinate and solve examples from the RAG database instead of the user's defined problem.

Resolution: Strict prompt engineering implementation. Instructions specify that the RAG context is strictly for syntax reference. The Gemini configuration enforces JSON output to maintain communication integrity with the frontend visualization logic.


config=types.GenerateContentConfig(

    system_instruction="Output strictly valid JSON. Override FORM inputs with TEXT inputs.",

    temperature=0.1,

    response_mime_type="application/json"

)


### 8.3. Client-Side Input Validation

All 11 form fields are validated in the browser before any request: the problem must be defined by text or image, numeric fields must be positive, Poisson's ratio is restricted to the physically meaningful range 0-0.5, and uploaded files are checked for image type and a 10 MB size limit. Number parsing accepts both decimal separators (0.29 and 0,29). This prevents malformed payloads from ever reaching the backend or the LLM.


### 8.4. AI Output Verification Layer

LLM output is never presented as trustworthy by default. The result page gives the user two independent ways to verify the generated script before running it:

- A free-body schematic (SVG, rendered client-side) built from the parameters the AI extracted, so the user can visually compare the AI's understanding against the original problem.

- A hand-calculated bending stress estimate using classical beam theory (sigma = 6M / b*h^2, with the bending moment formula selected per fixture type), compared against the material's yield strength to produce a factor-of-safety verdict (holds / near limit / fails).

If the AI-extracted parameters are absent or invalid, the frontend falls back to the form values, so the verification layer degrades gracefully instead of breaking.
<img width="1887" height="979" alt="result png" src="https://github.com/user-attachments/assets/288e4196-f28b-439d-b1c1-e4ce4a56089a" />



## 9. Architectural Decisions



- ADR-1: Adoption of FastAPI over Flask. Selected for its native asynchronous support, which is critical for managing LLM response latency, and its overall performance capabilities.

- ADR-2: Exclusion of Server-Side SolidWorks Execution. Due to the high resource requirements of CAD systems, the backend delegates simulation execution entirely to the user's local hardware rather than attempting costly cloud-based simulation.

- ADR-3: Frontend SVG Integration. Rather than requiring the AI to generate image files, the AI generates numerical parameters (JSON). The frontend utilizes JavaScript to render accurate and predictable SVG models based on established strength of materials formulas.

- ADR-4: Firebase Authentication over custom login. Password verification cannot be done safely in client-side code (all JavaScript is readable by the user), and the project has no server-side session storage. Delegating credential storage and verification to Firebase provides production-grade security at zero cost.

- ADR-5: multipart/form-data over a pure JSON body. The request must carry an optional binary image alongside structured parameters; JSON cannot transport binary files directly, so the contract is FormData with two fields: "data" (JSON string) and "image" (optional file).

- ADR-6: Separate result page with a sessionStorage handoff. Generation results are shown on result.html rather than inline, which keeps the form and the output as independent pages; app.html stores the generated code and extracted parameters in sessionStorage and redirects. The frontend accepts both backend response formats (plain-text script, or JSON with code + parsed parameters) for backward compatibility.



## 10. Quality Requirements



- Accuracy: The generated Python code must execute successfully in SolidWorks without throwing SyntaxError or COM Error exceptions.

- Resilience: The system must intercept and appropriately handle external API failures.

- Usability: The interface must provide the user with preliminary hand-calculated stress estimates prior to running the simulation.



## 11. Risks and Technical Debt



- SolidWorks API Volatility Risk: Updates to the COM interface in future Dassault Systemes releases may deprecate the local RAG knowledge base.

- Quota Vulnerability: Reliance on the free-tier Gemini API imposes a hard limit on system scalability (e.g., 20 requests per day).

- Recommendations for Future Improvement:

  1. Transition to a Pay-As-You-Go billing model for the Gemini API.

  2. Implement backend request caching (e.g., via Redis) to prevent querying the LLM for duplicate standard tasks.

  3. Expand the RAG knowledge base to support thermal and frequency analysis modules.



## 12. Glossary



- RAG (Retrieval-Augmented Generation): An architectural pattern that retrieves relevant contextual data to augment the prompt provided to a large language model.

- win32com: A Python library facilitating interaction with Windows COM objects, utilized here to interface programmatically with SolidWorks.

- Stress Check: A preliminary manual calculation of material stress (e.g., stress = M * c / I or stress = F / A) executed on the frontend to validate AI output parameters.

- CORS (Cross-Origin Resource Sharing): A browser security policy that blocks requests from a web page to a different domain unless the server explicitly allows them.

- FormData (multipart/form-data): A browser mechanism for sending structured fields and binary files in a single HTTP request.

- sessionStorage: Per-tab browser storage used here to pass the generated script and extracted parameters from the form page to the result page.

- Factor of Safety: The ratio of material yield strength to the estimated working stress; values well above 1 indicate the part should hold.
