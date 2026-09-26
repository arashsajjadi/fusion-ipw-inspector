"""Stock acquisition: how the in-process stock gets into the viewport.

``provider`` defines the common result type and the provider chain,
``post_export`` is the primary provider (Fusion's post engine exports the
in-process stock), ``saved_file`` the fallback (a file saved from Simulation),
``temporary_mesh`` owns the pickable temporary component and
``mesh_validation`` sanity-checks any mesh before it is trusted.
"""
