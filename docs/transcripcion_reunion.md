# Reunión de descubrimiento: portal de reservas de espacios

> Transcripción ficticia creada para el ejercicio. Los participantes se identifican
> por su función; no contiene datos de una reunión ni de una organización reales.

**Proyecto:** módulo de reservas para una aplicación web existente.  
**Participantes:** responsable de operaciones, responsable de producto y equipo técnico.  
**Objetivo:** acordar el alcance inicial y obtener una estimación provisional en horas de trabajo.

## Transcripción

**Producto:** Queremos que el equipo deje de gestionar las reservas de salas mediante
mensajes y hojas de cálculo. Ya tenemos una aplicación interna donde las personas
inician sesión. La propuesta es añadir una sección de reservas dentro de esa
aplicación. Hoy hablaremos de la primera entrega; las mejoras posteriores deben
quedar separadas para que podamos decidir qué priorizar.

**Operaciones:** Disponemos de ocho salas en una única sede. Las salas ya existen
como recursos en nuestra base de datos, con nombre, capacidad y ubicación. En la
primera versión queremos consultar disponibilidad por día y sala, reservar una
franja y consultar las reservas propias. La vista debe resultar cómoda desde un
ordenador y también desde el navegador de un móvil.

**Equipo técnico:** ¿Qué duración tienen las reservas y qué restricciones debemos
aplicar? Esto afecta a la disponibilidad, a la interfaz y a la prevención de
solapamientos. También necesitamos saber si una persona puede reservar para otra
o si cada reserva pertenece siempre al usuario que la crea.

**Operaciones:** Empecemos con franjas fijas de una hora, de lunes a viernes, entre
las ocho de la mañana y las ocho de la tarde. Una persona podrá reservar varias
franjas contiguas, pero cada franja será una reserva independiente. Solo podrá
reservar a su nombre. Todas las horas se mostrarán en la zona horaria de la sede,
Europe/Madrid; no habrá sedes internacionales en esta primera entrega.

**Producto:** La persona debería poder cambiar una reserva a otra franja disponible
y cancelarla. Para esta entrega, se podrá modificar o cancelar hasta dos horas
antes del inicio. Después de ese límite, la pantalla explicará que debe contactar
con operaciones. No necesitamos una lista de espera ni reservas periódicas, como
repetir la misma sala todos los martes.

**Equipo técnico:** Hay que proteger la disponibilidad también en el servidor. Si
dos personas intentan reservar la misma sala y franja a la vez, solo una operación
debe confirmarse. La otra recibirá un mensaje claro y podrá buscar otro horario.
Incluiremos pruebas de concurrencia, permisos y cambios de reserva. ¿Qué volumen
de uso deberíamos considerar como referencia?

**Operaciones:** La aplicación tiene aproximadamente doscientas cuentas activas.
En una mañana podrían coincidir veinte personas consultando o reservando. No
esperamos un lanzamiento público. El usuario verá el estado ocupado de una sala,
pero no necesitamos mostrar el nombre de la persona que la reservó. Operaciones
puede consultar actualmente los recursos desde sus herramientas internas.

**Producto:** Queremos un correo cuando se cree, modifique o cancele una reserva.
Debe mostrar sala, fecha, hora y un enlace para volver a la aplicación. En una
modificación, el correo debería dejar claro el horario anterior y el nuevo.
No incluimos recordatorios automáticos antes de la reunión ni mensajes por otros
canales. Los textos de los correos estarán únicamente en español.

**Equipo técnico:** Confirmamos que el proveedor de correo ya está contratado y
que el backend dispone de una cola para tareas en segundo plano. Aun así, habrá
que conectar estos eventos, preparar plantillas y probar los errores de envío.
Si el correo falla, la reserva debe seguir guardada y se debe registrar el fallo
para poder investigarlo. La confirmación en pantalla será la referencia inmediata
para el usuario.

**Operaciones:** Nos parece correcto. La autenticación, los perfiles de usuario y
los permisos básicos ya funcionan. No queremos desarrollar un nuevo sistema de
registro. Tampoco necesitamos un panel adicional para dar de alta salas, porque
ese mantenimiento se realiza fuera de este módulo. Quedaría pendiente decidir
cómo bloquear una sala por mantenimiento o por un festivo extraordinario.

**Producto:** Dejemos esa decisión como pregunta abierta. Para estimar podemos
suponer que los días no laborables y los cierres se facilitan mediante una
configuración sencilla del backend, pero hay que indicar cuánto depende de esa
suposición. Tampoco hemos acordado el plazo de conservación del historial; la
estimación debe recoger esa incertidumbre y no inventar una política definitiva.

**Equipo técnico:** Reutilizaremos el frontend, los componentes visuales y la base
de datos existentes. Hay un entorno de pruebas y un procedimiento de despliegue,
pero habrá que crear la tabla de reservas y su migración. El alcance incluye las
pantallas, los endpoints, la validación en ambos lados, las notificaciones y las
pruebas de integración. Necesitaremos revisar el diseño con producto antes de
cerrar la interfaz.

**Operaciones:** Para aceptar la entrega probaremos el recorrido completo: entrar,
buscar una sala libre, reservar, comprobar el correo, modificar y cancelar.
También verificaremos que no se puedan cambiar reservas de otras personas y que
dos solicitudes simultáneas no generen una reserva duplicada. Queremos una guía
breve para los usuarios y una demostración al equipo de operaciones.

**Producto:** Nos gustaría disponer de una primera versión en cuatro semanas,
pero es un objetivo de calendario, no un presupuesto de horas ya aprobado.
Pedimos una estimación con desglose, supuestos, exclusiones, riesgos, contingencia
y equipo recomendado. Debe distinguir esfuerzo de plazo y decir qué decisiones
faltan para mejorar la precisión. Quedan fuera pagos, facturación, aplicaciones
móviles nativas, sincronización con calendarios externos e importación de reservas
antiguas. Revisaremos la propuesta antes de comprometer una fecha de entrega.
