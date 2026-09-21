# Contributing

We welcome contributions! We have documented our development practices and guidelines at (Software Practices) [https://docs.allenneuraldynamics.org/en/latest/policies_practices/software_practices.html].

In general, we use uv, pytest, ruff, and interrogate for out local development.

### Installation

To install the dependencies needed for local development:
```bash
uv sync --group dev
```

To install the dependencies to build the documentation locally:
```bash
uv sync --group docs
```

### Linters and testing

- Please test your changes using the **pytest** library, which will run the tests and log a coverage report:

```bash
uv run pytest .
```

- Use **interrogate** to check that modules, methods, etc. have been documented thoroughly:

```bash
uv run interrogate .
```

- Use **ruff** to check that code is up to standards and auto-format:
```bash
uv run ruff check --fix
uv run ruff format
```

### Pull requests

For internal members, please create a branch. For external members, please fork the repository and open a pull request from the fork. We'll primarily use [Angular](https://github.com/angular/angular/blob/main/CONTRIBUTING.md#commit) style for commit messages. Roughly, they should follow the pattern:
```text
<type>(<scope>): <short summary>
```

where scope (optional) describes the packages affected by the code changes and type (mandatory) is one of:

- **build**: Changes that affect build tools or external dependencies (example scopes: pyproject.toml, setup.py)
- **ci**: Changes to our CI configuration files and scripts (examples: .github/workflows/ci.yml)
- **docs**: Documentation only changes
- **feat**: A new feature
- **fix**: A bugfix
- **perf**: A code change that improves performance
- **refactor**: A code change that neither fixes a bug nor adds a feature
- **test**: Adding missing tests or correcting existing tests

### Semantic Release

The table below, from [semantic release](https://github.com/semantic-release/semantic-release), shows which commit message gets you which release type when `semantic-release` runs (using the default configuration):

| Commit message                                                                                                                                                                                   | Release type                                                                                                    |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| `fix(pencil): stop graphite breaking when too much pressure applied`                                                                                                                             | ~~Patch~~ Fix Release, Default release                                                                          |
| `feat(pencil): add 'graphiteWidth' option`                                                                                                                                                       | ~~Minor~~ Feature Release                                                                                       |
| `perf(pencil): remove graphiteWidth option`<br><br>`BREAKING CHANGE: The graphiteWidth option has been removed.`<br>`The default graphite width of 10mm is always used for performance reasons.` | ~~Major~~ Breaking Release <br /> (Note that the `BREAKING CHANGE: ` token must be in the footer of the commit and need to be all caps as shown) |

### Documentation
To generate the rst files source files for documentation, run
```bash
uv run sphinx-apidoc -o docs/source/ src
```
Then to create the documentation HTML files, run
```bash
uv run sphinx-build -b html docs/source/ docs/build/html
```
More info on sphinx installation can be found [here](https://www.sphinx-doc.org/en/master/usage/installation.html).

