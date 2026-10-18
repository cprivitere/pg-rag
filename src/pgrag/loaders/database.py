class GameDatabase:
    def __init__(self):
        self.tables = {}
        self.wiki = {}

    def add_table(self, name, data):
        self.tables[name] = data

    def get_table(self, name):
        return self.tables.get(name, [])
