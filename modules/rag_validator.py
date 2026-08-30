def validate_rag(cfg, clients):
    client = clients["object_storage"]
    ns = client.get_namespace().data
    bucket = cfg.get("object_storage", "default_bucket")
    prefix = cfg.get("object_storage", "default_prefix")

    resp = client.list_objects(ns, bucket, prefix)

    if not resp.data.objects:
        raise Exception("No RAG documents found")

    print(f"✓ RAG validation: {len(resp.data.objects)} objects")
