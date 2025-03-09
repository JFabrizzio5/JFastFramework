import os
import inquirer

def crear_directorio_si_no_existe(ruta):
    if not os.path.exists(ruta):
        os.makedirs(ruta)

def crear_archivo_si_no_existe(ruta, contenido=""):
    if not os.path.exists(ruta):
        with open(ruta, 'w') as archivo:
            archivo.write(contenido)

def crear_modulo(nombre_modulo):
    base_path = os.path.join('mi-proyecto', nombre_modulo)
    directorios = ['controllers', 'models', 'routes', 'services']
    
    for directorio in directorios:
        ruta_directorio = os.path.join(base_path, directorio)
        crear_directorio_si_no_existe(ruta_directorio)
        crear_archivo_si_no_existe(os.path.join(ruta_directorio, '__init__.py'))

def crear_controlador(nombre_modulo, nombre_controlador):
    ruta_controlador = os.path.join('mi-proyecto', nombre_modulo, 'controllers', f'{nombre_controlador}.py')
    crear_archivo_si_no_existe(ruta_controlador, f'# Controlador {nombre_controlador}\n')

def crear_servicio(nombre_modulo, nombre_servicio):
    ruta_servicio = os.path.join('mi-proyecto', nombre_modulo, 'services', f'{nombre_servicio}.py')
    crear_archivo_si_no_existe(ruta_servicio, f'# Servicio {nombre_servicio}\n')

def obtener_modulos():
    base_path = 'mi-proyecto'
    return [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d))]

def menu_principal():
    preguntas = [
        inquirer.List('accion',
                      message="¿Qué te gustaría hacer?",
                      choices=['Crear nuevo módulo', 'Crear archivos en un módulo existente'],
                      ),
    ]
    respuestas = inquirer.prompt(preguntas)
    return respuestas['accion']

def menu_modulos():
    modulos = obtener_modulos()
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
                      choices=['Controlador', 'Servicio'],
                      ),
    ]
    respuestas = inquirer.prompt(preguntas)
    return respuestas['tipo_archivo']

def main():
    accion = menu_principal()
    
    if accion == 'Crear nuevo módulo':
        nombre_modulo = input("Introduce el nombre del nuevo módulo: ")
        crear_modulo(nombre_modulo)
        print(f"Módulo '{nombre_modulo}' creado exitosamente.")
    elif accion == 'Crear archivos en un módulo existente':
        modulo = menu_modulos()
        tipo_archivo = menu_archivos()
        
        if tipo_archivo == 'Controlador':
            nombre_controlador = input("Introduce el nombre del nuevo controlador: ")
            crear_controlador(modulo, nombre_controlador)
            print(f"Controlador '{nombre_controlador}' creado en el módulo '{modulo}' exitosamente.")
        elif tipo_archivo == 'Servicio':
            nombre_servicio = input("Introduce el nombre del nuevo servicio: ")
            crear_servicio(modulo, nombre_servicio)
            print(f"Servicio '{nombre_servicio}' creado en el módulo '{modulo}' exitosamente.")

if __name__ == "__main__":
    main()