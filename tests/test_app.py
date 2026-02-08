def test_index_route(client):
    resp = client.get('/')
    assert resp.status_code == 200


def test_update_params_route_exists(client):
    resp = client.post('/update_params', data={})
    # Either success or validation error, but the route must exist
    assert resp.status_code in (200, 400)
