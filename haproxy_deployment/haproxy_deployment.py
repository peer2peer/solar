import unittest

from x import db


class TestHAProxyDeployment(unittest.TestCase):
    def test_keystone_config(self):
        node1 = db.get_resource('node1')
        node2 = db.get_resource('node2')
        keystone1 = db.get_resource('keystone1')
        keystone2 = db.get_resource('keystone2')

        self.assertEqual(keystone1.args['ip'], node1.args['ip'])
        self.assertEqual(keystone2.args['ip'], node2.args['ip'])

    def test_haproxy_keystone_config(self):
        keystone1 = db.get_resource('keystone1')
        keystone2 = db.get_resource('keystone2')
        haproxy_keystone_config = db.get_resource('haproxy_keystone_config')

        self.assertDictEqual(
            haproxy_keystone_config.args['servers'],
            {
                'keystone1': keystone1.args['ip'],
                'keystone2': keystone2.args['ip'],
            }
        )

    def test_nova_config(self):
        node3 = db.get_resource('node3')
        node4 = db.get_resource('node4')
        nova1 = db.get_resource('nova1')
        nova2 = db.get_resource('nova2')

        self.assertEqual(nova1.args['ip'], node3.args['ip'])
        self.assertEqual(nova2.args['ip'], node4.args['ip'])

    def test_haproxy_nova_config(self):
        nova1 = db.get_resource('nova1')
        nova2 = db.get_resource('nova2')
        haproxy_nova_config = db.get_resource('haproxy_nova_config')

        self.assertDictEqual(
            haproxy_nova_config.args['servers'],
            {
                'nova1': nova1.args['ip'],
                'nova2': nova2.args['ip'],
            }
        )

    def test_haproxy(self):
        node5 = db.get_resource('node5')
        haproxy = db.get_resource('haproxy')

        self.assertEqual(node5.args['ip'], haproxy.args['ip'])


def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestHAProxyDeployment)
    unittest.TextTestRunner().run(suite)