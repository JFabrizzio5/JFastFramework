import os
import subprocess
import inquirer

def crear_directorio_si_no_existe(ruta):
    if not os.path.exists(ruta):
        os.makedirs(ruta)

def crear_archivo_si_no_existe(ruta, contenido=""):
    if not os.path.exists(ruta):
        with open(ruta, 'w') as archivo:
            archivo.write(contenido)

def formatear_nombre(nombre):
    return ''.join([palabra.capitalize() for palabra in nombre.split()])

def crear_modulo(proyecto, nombre_modulo):
    nombre_modulo = formatear_nombre(nombre_modulo)
    base_path = os.path.join(proyecto, nombre_modulo)
    directorios = ['controllers', 'models', 'routes', 'services', 'repositories']
    
    for directorio in directorios:
        ruta_directorio = os.path.join(base_path, directorio)
        crear_directorio_si_no_existe(ruta_directorio)
        crear_archivo_si_no_existe(os.path.join(ruta_directorio, '__init__.py'))

def crear_controlador(proyecto, nombre_modulo, nombre_controlador):
    nombre_controlador = formatear_nombre(nombre_controlador)
    ruta_controlador = os.path.join(proyecto, nombre_modulo, 'controllers', f'{nombre_controlador}.py')
    crear_archivo_si_no_existe(ruta_controlador, f'# Controlador {nombre_controlador}\n')

def crear_servicio(proyecto, nombre_modulo, nombre_servicio):
    nombre_servicio = formatear_nombre(nombre_servicio)
    ruta_servicio = os.path.join(proyecto, nombre_modulo, 'services', f'{nombre_servicio}.py')
    crear_archivo_si_no_existe(ruta_servicio, f'# Servicio {nombre_servicio}\n')

def crear_modelo(proyecto, nombre_modulo, nombre_modelo):
    nombre_modelo = formatear_nombre(nombre_modelo)
    ruta_modelo = os.path.join(proyecto, nombre_modulo, 'models', f'{nombre_modelo}.py')
    crear_archivo_si_no_existe(ruta_modelo, f'# Modelo {nombre_modelo}\n')

def crear_ruta(proyecto, nombre_modulo, nombre_ruta):
    nombre_ruta = formatear_nombre(nombre_ruta)
    ruta_ruta = os.path.join(proyecto, nombre_modulo, 'routes', f'{nombre_ruta}.py')
    crear_archivo_si_no_existe(ruta_ruta, f'# Ruta {nombre_ruta}\n')

def crear_repositorio(proyecto, nombre_modulo, nombre_repositorio):
    nombre_repositorio = formatear_nombre(nombre_repositorio)
    ruta_repositorio = os.path.join(proyecto, nombre_modulo, 'repositories', f'{nombre_repositorio}.py')
    crear_archivo_si_no_existe(ruta_repositorio, f'# Repositorio {nombre_repositorio}\n')

def obtener_proyectos():
    base_path = os.path.dirname(os.path.abspath(__file__))
    return [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d))]

def obtener_modulos(proyecto):
    base_path = proyecto
    return [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d))]

def menu_principal():
    preguntas = [
        inquirer.List('accion',
                      message="¿Qué te gustaría hacer?",
                      choices=['Crear nuevo proyecto', 'Crear nuevo módulo', 'Crear archivos en un módulo existente'],
                      ),
    ]
    respuestas = inquirer.prompt(preguntas)
    return respuestas['accion']

def menu_proyectos():
    proyectos = obtener_proyectos()
    preguntas = [
        inquirer.List('proyecto',
                      message="Selecciona un proyecto",
                      choices=proyectos,
                      ),
    ]
    respuestas = inquirer.prompt(preguntas)
    return respuestas['proyecto']

def menu_modulos(proyecto):
    modulos = obtener_modulos(proyecto)
    preguntas = [
        inquirer.List('modulo',
                      message="Selecciona un módulo",
                      choices=modulos,
                      ),
    ]
    respuestas = inquirer.prompt(preguntas)
    return respuestas['modulo']

def menu_archivos():
    preguntas = [
        inquirer.List('tipo_archivo',
                      message="¿Qué tipo de archivo te gustaría crear?",
                      choices=['Controlador', 'Servicio', 'Modelo', 'Ruta', 'Repositorio'],
                      ),
    ]
    respuestas = inquirer.prompt(preguntas)
    return respuestas['tipo_archivo']

def crear_estructura_proyecto(nombre_proyecto):
    # Crear main.py
    main_py_contenido = '''from fastapi import FastAPI
from example.routes.example import router as example_router

app = FastAPI()

app.include_router(example_router, prefix="/example", tags=["example"])

@app.get("/")
def read_root():
    return {"message": "Hello World"}
'''
    crear_archivo_si_no_existe(os.path.join(nombre_proyecto, 'main.py'), main_py_contenido)

    # Crear ejemplo de módulo
    crear_modulo(nombre_proyecto, 'example')

    # Crear archivos del módulo de ejemplo
    crear_archivo_si_no_existe(os.path.join(nombre_proyecto, 'example', 'models', 'example_model.py'), 'class ExampleModel:\n    pass\n')
    crear_archivo_si_no_existe(os.path.join(nombre_proyecto, 'example', 'repositories', 'example_repository.py'), 'class ExampleRepository:\n    def get_example(self):\n        return {"message": "Hello from the repository"}\n')
    crear_archivo_si_no_existe(os.path.join(nombre_proyecto, 'example', 'services', 'example_service.py'), 'from example.repositories.example_repository import ExampleRepository\n\nclass ExampleService:\n    def __init__(self):\n        self.repository = ExampleRepository()\n\n    def get_example(self):\n        return self.repository.get_example()\n')
    crear_archivo_si_no_existe(os.path.join(nombre_proyecto, 'example', 'controllers', 'example_controller.py'), 'from example.services.example_service import ExampleService\n\nclass ExampleController:\n    def __init__(self):\n        self.service = ExampleService()\n\n    def get_example(self):\n        return self.service.get_example()\n')
    crear_archivo_si_no_existe(os.path.join(nombre_proyecto, 'example', 'routes', 'example.py'), 'from fastapi import APIRouter\nfrom example.controllers.example_controller import ExampleController\n\nrouter = APIRouter()\n\ncontroller = ExampleController()\n\n@router.get("/")\ndef get_example():\n    return controller.get_example()\n')

    # Crear requirements.txt
    requirements_contenido = '''fastapi
uvicorn
motor
redis
pandas
'''
    crear_archivo_si_no_existe(os.path.join(nombre_proyecto, 'requirements.txt'), requirements_contenido)

    # Crear .venv
    subprocess.run(['python', '-m', 'venv', os.path.join(nombre_proyecto, '.venv')])

    # Actualizar pip e instalar dependencias
    subprocess.run([os.path.join(nombre_proyecto, '.venv', 'Scripts', 'pip'), 'install', '--upgrade', 'pip'])
    subprocess.run([os.path.join(nombre_proyecto, '.venv', 'Scripts', 'pip'), 'install', '-r', os.path.join(nombre_proyecto, 'requirements.txt')])

def main():
    accion = menu_principal()
    
    if accion == 'Crear nuevo proyecto':
        nombre_proyecto = input("Introduce el nombre del nuevo proyecto: ")
        nombre_proyecto = formatear_nombre(nombre_proyecto)
        crear_directorio_si_no_existe(nombre_proyecto)
        crear_estructura_proyecto(nombre_proyecto)
        print(f"Proyecto '{nombre_proyecto}' creado exitosamente.")
    elif accion == 'Crear nuevo módulo':
        proyecto = menu_proyectos()
        nombre_modulo = input("Introduce el nombre del nuevo módulo: ")
        crear_modulo(proyecto, nombre_modulo)
        print(f"Módulo '{nombre_modulo}' creado en el proyecto '{proyecto}' exitosamente.")
    elif accion == 'Crear archivos en un módulo existente':
        proyecto = menu_proyectos()
        modulo = menu_modulos(proyecto)
        tipo_archivo = menu_archivos()
        
        if tipo_archivo == 'Controlador':
            nombre_controlador = input("Introduce el nombre del nuevo controlador: ")
            crear_controlador(proyecto, modulo, nombre_controlador)
            print(f"Controlador '{nombre_controlador}' creado en el módulo '{modulo}' del proyecto '{proyecto}' exitosamente.")
        elif tipo_archivo == 'Servicio':
            nombre_servicio = input("Introduce el nombre del nuevo servicio: ")
            crear_servicio(proyecto, modulo, nombre_servicio)
            print(f"Servicio '{nombre_servicio}' creado en el módulo '{modulo}' del proyecto '{proyecto}' exitosamente.")
        elif tipo_archivo == 'Modelo':
            nombre_modelo = input("Introduce el nombre del nuevo modelo: ")
            crear_modelo(proyecto, modulo, nombre_modelo)
            print(f"Modelo '{nombre_modelo}' creado en el módulo '{modulo}' del proyecto '{proyecto}' exitosamente.")
        elif tipo_archivo == 'Ruta':
            nombre_ruta = input("Introduce el nombre de la nueva ruta: ")
            crear_ruta(proyecto, modulo, nombre_ruta)
            print(f"Ruta '{nombre_ruta}' creada en el módulo '{modulo}' del proyecto '{proyecto}' exitosamente.")
        elif tipo_archivo == 'Repositorio':
            nombre_repositorio = input("Introduce el nombre del nuevo repositorio: ")
            crear_repositorio(proyecto, modulo, nombre_repositorio)
            print(f"Repositorio '{nombre_repositorio}' creado en el módulo '{modulo}' del proyecto '{proyecto}' exitosamente.")

if __name__ == "__main__":
    main()