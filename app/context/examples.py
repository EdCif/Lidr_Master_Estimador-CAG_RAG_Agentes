"""Ejemplos ficticios de estimaciones para el contexto CAG del sistema.

Cada ejemplo relaciona una petición con una estimación de referencia.
Las cifras representan horas de trabajo de una persona.
Se incorporarán al prompt cuando implementemos el servicio de estimación.
"""


ESTIMATION_EXAMPLES = [
    {
        "id": "Gestion_Tesoreria",
        "Requerimiento Cliente XXXX": (
            "Generar una Plataforma para gestionar la tesorería de la compañía. "
            "Evaluar cada funcionalidad por separado teniendo en cuenta que debe ser multiusuario y contar con seguridad LDAP. "
            "Tendrá un nombre único. "
            "El proyecto deberá tener una duarción máxima de 4,5 meses, 320 horas. "
            "consultar, listar con paginación, modificar y eliminar categorías. "
            "No se permitirá eliminar categorías que tengan productos asociados."
        ),
        "Estimaciones": {
            "Complejidad": "media",
            "Unidades dedicación (FTEs)": "horas_persona",
            "Asunciones": [
                "La base de datos y las integraciones de datos y migraciones entre entornos ya funcionan.",
                "La autenticación y el rol de administrador ya están implementados con doble factor MFA.",
                "Existe un entorno de pruebas y un procedimiento de despliegue.",
            ],
            "Exclusiones de proyecto": [
                "Generación y cuadre del modelo de datos.",
                "Generación de ficheros de enlace",
                "Dotación o Creación de infraestructura nueva.",
            ],
            "Desglose de Tareas": [
                {"task": "Project Managment", "hours": 30},
                {"task": "Diseño UI/UIX", "hours": 25},
                {"task": "Generación de Módulo Real", "hours": 25},
                {"task": "Generación de Módulo Budget", "hours": 25},
                {"task": "Generación de Módulo Alertas Cash", "hours": 10},
                {"task": "Endpoints CRUD", "hours": 10},
                {"task": "Validaciones y uso de permisos", "hours": 10},
                {"task": "Pruebas de operaciones y documentación casos de error", "hours": 20},
                {"task": "Formación usuarios", "hours": 8},
                {"task": "Margen para ajustes menores durante la integración", "hours": 20},
            ],
            "Total Horas": 320,
            "Equipo recomendado": "1 senior project manager + 2 desarrolladores full stack + 1 diseñador UX (part time)",
            "Riesgos": [
                "Si los permisos o las relaciones con productos requieren cambios, "
                "habrá que revisar el alcance y la estimación.",
            ],
        },
    },
    {
        "id": "Gestion_Reservas",
        "Requerimiento Cliente ZZZZ": (
            "Añadir un módulo de reservas a una aplicación web existente. "
            "Los usuarios podrán consultar horarios disponibles y crear, "
            "modificar o cancelar sus reservas desde una interfaz web. Cada "
            "reserva ocupará un recurso en una franja horaria. El sistema deberá "
            "impedir reservas solapadas, incluso con solicitudes simultáneas, "
            "y enviar un correo de confirmación al crear, modificar o cancelar."
        ),
        "Estimaciones": {
            "Complejidad": "media",
            "Unidades dedicación (FTEs)": "horas_persona",
            "Asunciones": [
                "Existen frontend, backend, base de datos y autenticación de usuarios.",
                "Se utiliza una única zona horaria y franjas de duración fija.",
                "Los recursos reservables ya están dados de alta en la aplicación.",
                "Hay un proveedor de correo y un mecanismo de tareas en segundo plano configurados.",
                "Las reglas de disponibilidad y cancelación se acuerdan antes del desarrollo.",
            ],
            "Exclusiones": [
                "Pagos, reservas recurrentes y sincronización con calendarios externos.",
                "Aplicaciones móviles nativas y nuevos paneles de administración.",
            ],
            "Desglose de Tareas": [
                {"task": "Definición de reglas y diseño del flujo de reserva", "hours": 6},
                {"task": "Modelo de reservas y migraciones", "hours": 6},
                {"task": "API de disponibilidad y reservas con control de concurrencia", "hours": 16},
                {"task": "Interfaz de disponibilidad y gestión de reservas propias", "hours": 16},
                {"task": "Notificaciones por correo y tratamiento de fallos de envío", "hours": 6},
                {"task": "Pruebas de integración, permisos, solapamientos y concurrencia", "hours": 14},
                {"task": "Documentación y verificación en el entorno de despliegue", "hours": 4},
                {"task": "Margen para ajustes de integración y casos límite", "hours": 12},
            ],
            "Total Horas": 80,
            "Equipo recomendado": "2 desarrolladores full stack",
            "Riesgos": [
                "Añadir varias zonas horarias o duraciones variables cambia las "
                "reglas de disponibilidad y requiere revisar la estimación.",
                "Cambios en las reglas de cancelación pueden afectar a la API, "
                "la interfaz, los correos y las pruebas.",
            ],
        },
    },
]
